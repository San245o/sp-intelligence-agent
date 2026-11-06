"""Distinguish evidence about a company from the company's own website."""
from urllib.parse import urlsplit

DIRECTORY_DOMAINS = {
    "brreg.no", "proff.no", "purehelp.no", "1881.no", "gulesider.no", "firmalisten.no",
    "companywall.no", "firmadatabasen.no", "sokfirma.no", "yra.no", "northdata.com",
    "nor47business.com", "regnskapstall.no", "vexter.no", "byndle.no", "draupner.com", "fagfolkguiden.no",
    "sgregister.dibk.no", "karrierestart.no", "dnb.com",
    "norgelei.no", "utdanning.no", "northdata.de", "largestcompanies.com",
}


def directory_url(url):
    host = (urlsplit(str(url or "")).hostname or "").casefold()
    return any(host == domain or host.endswith('.' + domain) for domain in DIRECTORY_DOMAINS)
