import csv
import ipaddress
import json
import re
import socket
import sys
import time
from urllib.parse import urljoin, urlparse
from xml.etree import ElementTree as ET

import requests
from bs4 import BeautifulSoup

# ------------------------------------------------------------------ CONFIG --

INPUT_CSV = "websites.csv"        # must have headers: name, website
OUTPUT_JSON = "output.json"

MAX_PAGES_TO_CRAWL = 25           # cap for fallback crawler, per site
MAX_SITEMAP_URLS = 200            # cap on how many sitemap URLs we collect
MAX_PAGES_TO_SCAN_FOR_EMAIL = 8   # how many pages per site we check for emails

REQUEST_TIMEOUT = 10              # seconds
DELAY_BETWEEN_REQUESTS = 1.0      # politeness delay, seconds

USER_AGENT = "Mozilla/5.0 (compatible; InfoBot/1.0; contact-scraper)"
HEADERS = {"User-Agent": USER_AGENT}

MAX_RESPONSE_BYTES = 5 * 1024 * 1024   # 5 MB cap per fetched page
MAX_REDIRECTS = 5


class _BlockedURL(Exception):
    """Raised internally when a URL/IP fails the SSRF safety check."""
    pass


def _is_unsafe_ip(ip_str: str) -> bool:
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return True  # can't parse it -> treat as unsafe
    return (
        ip.is_private or ip.is_loopback or ip.is_link_local
        or ip.is_reserved or ip.is_multicast or ip.is_unspecified
    )


def _assert_url_is_safe(url: str):
    """Raises _BlockedURL if the URL's scheme is not http/https, or if ANY
    IP that its hostname resolves to is private/loopback/link-local/reserved.
    This is checked fresh on every hop of a redirect chain."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise _BlockedURL(f"Blocked scheme: {parsed.scheme!r}")
    hostname = parsed.hostname
    if not hostname:
        raise _BlockedURL("No hostname in URL")
    try:
        addrinfo = socket.getaddrinfo(hostname, None)
    except socket.gaierror as e:
        raise _BlockedURL(f"DNS resolution failed: {e}")
    for family, _, _, _, sockaddr in addrinfo:
        ip_str = sockaddr[0]
        if _is_unsafe_ip(ip_str):
            raise _BlockedURL(f"Blocked unsafe IP {ip_str} for host {hostname}")

EMAIL_REGEX = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")

# Common placeholder / junk domains to filter out of results
EMAIL_BLACKLIST_DOMAINS = {
    "example.com", "domain.com", "yourdomain.com", "email.com",
    "sentry.io", "wixpress.com", "godaddy.com", "schema.org",
}

ABOUT_KEYWORDS = ["about-us", "about_us", "aboutus", "about", "who-we-are", "our-story", "company"]
CONTACT_KEYWORDS = ["contact-us", "contact_us", "contactus", "contact"]

SITEMAP_NS = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}


# ----------------------------------------------------------------- HELPERS --

def normalize_url(url: str) -> str:
    url = (url or "").strip()
    if not url:
        return url
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    return url.rstrip("/")


def get_domain(url: str) -> str:
    return urlparse(url).netloc


def normalize_website_for_dedupe(url: str) -> str:
    """Normalizes a website URL so that http/https, www/no-www, and
    trailing-slash variants of the same site compare equal. Used by the
    'find duplicate leads' feature — two leads with the same normalized
    website are considered the same business.
    """
    if not url:
        return ""
    u = url.strip().lower()
    u = re.sub(r"^https?://", "", u)
    u = re.sub(r"^www\.", "", u)
    u = u.split("?")[0].split("#")[0]
    u = u.rstrip("/")
    return u


def fetch(url: str, timeout: int = REQUEST_TIMEOUT):
    """GET a URL, return the response object or None on any failure.

    SSRF-safe: validates scheme + resolves the hostname to an IP and
    rejects private/loopback/link-local/reserved addresses BEFORE the
    request is made, and re-checks on every redirect hop (a public URL
    that redirects to an internal one is blocked, not silently followed).
    Also caps the response body size.
    """
    current_url = url
    for _ in range(MAX_REDIRECTS + 1):
        try:
            _assert_url_is_safe(current_url)
        except _BlockedURL as e:
            print(f"[SSRF-BLOCKED] {current_url} -> {e}", flush=True)
            return None

        try:
            resp = requests.get(
                current_url,
                headers=HEADERS,
                timeout=timeout,
                allow_redirects=False,   # we follow manually so each hop gets re-checked
                stream=True,
            )
        except requests.RequestException:
            return None

        if resp.status_code in (301, 302, 303, 307, 308):
            location = resp.headers.get("Location")
            resp.close()
            if not location:
                return None
            current_url = urljoin(current_url, location)
            continue

        if resp.status_code != 200:
            resp.close()
            return None

        # Enforce a size cap while reading the body
        try:
            content_length = int(resp.headers.get("Content-Length", 0) or 0)
        except ValueError:
            content_length = 0
        if content_length and content_length > MAX_RESPONSE_BYTES:
            resp.close()
            return None

        chunks = []
        total = 0
        for chunk in resp.iter_content(chunk_size=65536):
            total += len(chunk)
            if total > MAX_RESPONSE_BYTES:
                resp.close()
                return None
            chunks.append(chunk)
        resp._content = b"".join(chunks)
        resp.close()
        return resp

    print(f"[SSRF-BLOCKED] {url} -> too many redirects", flush=True)
    return None


def clean_text(html: str) -> str:
    """Strip scripts/styles and collapse whitespace to get readable page text."""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "svg"]):
        tag.decompose()
    text = soup.get_text(separator=" ")
    return re.sub(r"\s+", " ", text).strip()


def extract_emails(raw_text: str) -> set:
    found = set()
    for match in EMAIL_REGEX.findall(raw_text):
        email = match.strip().strip(".").lower()
        domain = email.split("@")[-1]
        if domain in EMAIL_BLACKLIST_DOMAINS:
            continue
        if re.search(r"\.(png|jpg|jpeg|gif|svg|webp|css|js)$", email):
            continue  # filters false positives like name@2x.png
        found.add(email)
    return found


# ------------------------------------------------------------ SITEMAP LOGIC --

def find_sitemap_locations(base_url: str) -> list:
    """Return candidate sitemap URLs discovered via robots.txt or common paths."""
    candidates = []

    robots_resp = fetch(urljoin(base_url + "/", "robots.txt"))
    if robots_resp:
        for line in robots_resp.text.splitlines():
            line = line.strip()
            if line.lower().startswith("sitemap:"):
                sm_url = line.split(":", 1)[1].strip()
                if sm_url:
                    candidates.append(sm_url)

    if not candidates:
        for path in ("/sitemap.xml", "/sitemap_index.xml", "/sitemap-index.xml", "/sitemap1.xml"):
            candidates.append(base_url + path)

    valid = []
    for sm_url in candidates:
        resp = fetch(sm_url)
        if not resp:
            continue
        content_type = resp.headers.get("Content-Type", "")
        head = resp.text[:2000]
        if "xml" in content_type or head.strip().startswith("<?xml") or "<urlset" in head or "<sitemapindex" in head:
            valid.append(sm_url)
    return valid


def parse_sitemap(sitemap_url: str, collected: set, depth: int = 0):
    """Recursively parse a sitemap, following nested sitemap-index files."""
    if depth > 3 or len(collected) >= MAX_SITEMAP_URLS:
        return
    resp = fetch(sitemap_url)
    if not resp:
        return
    try:
        root = ET.fromstring(resp.content)
    except ET.ParseError:
        return

    tag = root.tag.lower()

    if tag.endswith("sitemapindex"):
        for sitemap_el in root.findall("sm:sitemap", SITEMAP_NS):
            loc = sitemap_el.find("sm:loc", SITEMAP_NS)
            if loc is not None and loc.text:
                parse_sitemap(loc.text.strip(), collected, depth + 1)
                if len(collected) >= MAX_SITEMAP_URLS:
                    return
    elif tag.endswith("urlset"):
        for url_el in root.findall("sm:url", SITEMAP_NS):
            loc = url_el.find("sm:loc", SITEMAP_NS)
            if loc is not None and loc.text:
                collected.add(loc.text.strip())
                if len(collected) >= MAX_SITEMAP_URLS:
                    return


def get_urls_from_sitemap(base_url: str) -> list:
    collected = set()
    for sm_url in find_sitemap_locations(base_url):
        parse_sitemap(sm_url, collected)
        if len(collected) >= MAX_SITEMAP_URLS:
            break
    return list(collected)


# -------------------------------------------------------- FALLBACK CRAWLER --

def crawl_site(base_url: str, max_pages: int = MAX_PAGES_TO_CRAWL) -> list:
    """Simple breadth-first crawl over internal links (used when no sitemap exists)."""
    domain = get_domain(base_url)
    visited = set()
    queue = [base_url]
    found_urls = []

    while queue and len(visited) < max_pages:
        url = queue.pop(0)
        if url in visited:
            continue
        visited.add(url)

        resp = fetch(url)
        time.sleep(DELAY_BETWEEN_REQUESTS)
        if not resp:
            continue
        found_urls.append(url)

        try:
            soup = BeautifulSoup(resp.text, "html.parser")
        except Exception:
            continue

        for a in soup.find_all("a", href=True):
            full_url = urljoin(url, a["href"]).split("#")[0]
            parsed = urlparse(full_url)
            if parsed.netloc != domain:
                continue  # stay on-site
            if full_url not in visited and full_url not in queue:
                if len(visited) + len(queue) < max_pages:
                    queue.append(full_url)

    return found_urls


# ------------------------------------------------------------ PAGE PICKING --

def pick_url_by_keywords(urls: list, keywords: list):
    for url in urls:
        path = urlparse(url).path.lower()
        if any(kw in path for kw in keywords):
            return url
    return None


# --------------------------------------------------------- SCRAPE ONE SITE --

def scrape_site(name: str, website: str) -> dict:
    base_url = normalize_url(website)
    result = {"name": name, "website": website, "email": "", "about_text": ""}

    if not base_url:
        return result

    print(f"  -> discovering URLs for {base_url}")
    urls = get_urls_from_sitemap(base_url)
    source = "sitemap"

    if not urls:
        print("  -> no sitemap found, crawling site instead")
        urls = crawl_site(base_url)
        source = "crawl"

    if base_url not in urls:
        urls.insert(0, base_url)

    print(f"  -> found {len(urls)} url(s) via {source}")

    about_url = pick_url_by_keywords(urls, ABOUT_KEYWORDS)
    contact_url = pick_url_by_keywords(urls, CONTACT_KEYWORDS)

    pages_to_scan = [base_url]
    for u in (about_url, contact_url):
        if u and u not in pages_to_scan:
            pages_to_scan.append(u)
    for u in urls:
        if len(pages_to_scan) >= MAX_PAGES_TO_SCAN_FOR_EMAIL:
            break
        if u not in pages_to_scan:
            pages_to_scan.append(u)

    emails_found = set()
    about_text = ""

    for page_url in pages_to_scan:
        resp = fetch(page_url)
        time.sleep(DELAY_BETWEEN_REQUESTS)
        if not resp:
            continue
        emails_found |= extract_emails(resp.text)
        if page_url == about_url:
            about_text = clean_text(resp.text)

    # in case the about page wasn't in the email-scan batch for some reason
    if about_url and not about_text:
        resp = fetch(about_url)
        if resp:
            about_text = clean_text(resp.text)

    result["email"] = ", ".join(sorted(emails_found))
    result["about_text"] = about_text
    return result


def scrape_website(website: str) -> dict:
    """Thin wrapper around scrape_site() for callers (like the Flask app)
    that only have a website URL and don't need the CSV row's name/website
    echoed back. Always returns a dict with 'email' and 'about_text' keys
    (both '' on failure — never raises for a bad/unreachable URL, but CAN
    raise on programmer error, e.g. network layer misconfiguration, so
    callers should still wrap this in a try/except).

    Returns:
        {"email": "a@b.com, c@d.com" | "", "about_text": "..." | ""}
    """
    data = scrape_site("", website)
    return {"email": data.get("email", ""), "about_text": data.get("about_text", "")}


# ------------------------------------------------------------------- MAIN --

def load_websites_from_csv(csv_path: str) -> list:
    sites = []
    with open(csv_path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames or "website" not in [c.strip().lower() for c in reader.fieldnames]:
            print(f"ERROR: {csv_path} must have a header row with 'name' and 'website' columns.")
            sys.exit(1)
        for row in reader:
            name = (row.get("name") or "").strip()
            website = (row.get("website") or "").strip()
            if website:
                sites.append({"name": name, "website": website})
    return sites


def save_json(data: list, path: str):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def main():
    sites = load_websites_from_csv(INPUT_CSV)
    if not sites:
        print(f"No websites found in {INPUT_CSV}.")
        sys.exit(1)

    results = []
    total = len(sites)

    for i, site in enumerate(sites, start=1):
        print(f"[{i}/{total}] {site['name']} — {site['website']}")
        try:
            data = scrape_site(site["name"], site["website"])
        except Exception as exc:
            print(f"  !! error: {exc}")
            data = {"name": site["name"], "website": site["website"], "email": "", "about_text": ""}
        results.append(data)
        save_json(results, OUTPUT_JSON)  # save progress after every site

    print(f"\nDone. {len(results)} site(s) processed -> {OUTPUT_JSON}")


if __name__ == "__main__":
    main()