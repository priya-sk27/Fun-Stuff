"""
13F CUSIP Lookup Web App
Accepts a CUSIP, looks it up via OpenFIGI + yfinance, and displays enriched
Investment Strategy fields — no Salesforce writes required.
"""

import os
import time
import requests
import yfinance as yf
from datetime import datetime
from flask import Flask, render_template, request, jsonify
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)

OPENFIGI_URL = "https://api.openfigi.com/v3/mapping"
OPENFIGI_KEY = os.getenv("OPENFIGI_API_KEY", "")

# ── Picklist maps ─────────────────────────────────────────────────────────────

PRODUCT_STRUCTURE_MAP = {
    "Common Stock":          "Common Stock",
    "ETP":                   "ETF",
    "ETF":                   "ETF",
    "Mutual Fund":           "Mutual Fund",
    "Closed-End Fund":       "Closed-End Fund",
    "Unit Investment Trust": "Mutual Fund",
    "Preferred Stock":       "Preferred Stock",
    "ADR":                   "Common Stock",
    "Corporate":             "Separate Account",
    "Government":            "Separate Account",
    "Warrant":               "Other",
    "Right":                 "Other",
    "Index":                 "Other",
}

ASSET_CLASS_KEYWORDS = {
    "fixed income":     "US Fixed Income",
    "fixed rate":       "US Fixed Income",
    "bond":             "US Fixed Income",
    "bonds":            "US Fixed Income",
    "treasury":         "US Fixed Income",
    "credit":           "US Fixed Income",
    "mbs":              "US Fixed Income",
    "mortgage":         "US Fixed Income",
    "aggregate":        "US Fixed Income",
    "corporate bond":   "US Fixed Income",
    "municipal":        "US Fixed Income",
    "muni":             "US Fixed Income",
    "government":       "US Fixed Income",
    "high yield":       "US Fixed Income",
    "inflation":        "US Fixed Income",
    "tips":             "US Fixed Income",
    "real estate":      "Real Assets",
    "reit":             "Real Assets",
    "infrastructure":   "Real Assets",
    "commodity":        "Real Assets",
    "commodities":      "Real Assets",
    "energy":           "Real Assets",
    "natural resources":"Real Assets",
    "gold":             "Real Assets",
    "private equity":   "Private Equity",
    "buyout":           "Private Equity",
    "venture":          "Venture Capital",
    "hedge":            "Hedge Funds/Liquid Alternatives",
    "absolute return":  "Hedge Funds/Liquid Alternatives",
    "multi-asset":      "Multi-Asset",
    "balanced":         "Multi-Asset",
    "allocation":       "Multi-Asset",
    "emerging market":  "Emerging Market Equities",
    "emerging markets": "Emerging Market Equities",
    "international":    "International Equities",
    "global":           "Global Equities",
    "world":            "Global Equities",
    "europe":           "Global Equities",
    "asia":             "Global Equities",
    "china":            "Global Equities",
    "japan":            "Global Equities",
}

# yfinance category → Sub_Asset_Class__c (Salesforce picklist values)
SUB_ASSET_CLASS_MAP = {
    "intermediate government":          "Mortgage Backed",
    "intermediate core bond":           "Intermediate Core Bond",
    "intermediate core-plus bond":      "Intermediate Core Bond",
    "short-term bond":                  "Short Duration Bond",
    "ultrashort bond":                  "Ultrashort Bond",
    "long-term bond":                   "Long Term Bond",
    "long government":                  "Long Government Bond",
    "corporate bond":                   "Investment Grade Corporate",
    "high yield bond":                  "High Yield Bond",
    "inflation-protected bond":         "Inflation Protected Bond",
    "multisector bond":                 "Multi-Sector Bond",
    "nontraditional bond":              "Nontraditional Bond",
    "bank loan":                        "Bank Loan",
    "convertibles":                     "Convertibles",
    "preferred stock":                  "Preferred",
    "large blend":                      "Large Cap Blend",
    "large growth":                     "Large Cap Growth",
    "large value":                      "Large Cap Value",
    "mid-cap blend":                    "Mid Cap Blend",
    "mid-cap growth":                   "Mid Cap Growth",
    "mid-cap value":                    "Mid Cap Value",
    "small blend":                      "Small Cap Blend",
    "small growth":                     "Small Cap Growth",
    "small value":                      "Small Cap Value",
    "foreign large blend":              "International Large Blend",
    "foreign large growth":             "International Large Cap Growth",
    "foreign large value":              "International Large Cap Value",
    "foreign small/mid blend":          "International Small Cap Core",
    "diversified emerging markets":     "Emerging Market Equities",
    "emerging markets bond":            "Emerging Markets Debt",
    "world bond":                       "International/Global Fixed Income",
    "global real estate":               "World Real Estate",
    "real estate":                      "REITs",
    "health":                           "Healthcare",
    "technology":                       "Technology",
    "energy limited partnership":       "Energy",
    "commodities broad basket":         "Commodities",
    "commodities focused":              "Commodities",
    "precious metals":                  "Precious Metals",
    "managed futures":                  "Managed Futures",
    "multialternative":                 "Multi-Alternative",
    "long-short equity":                "Long/Short Equity",
    "market neutral":                   "Market Neutral",
    "global macro":                     "Global Macro",
    "target-date":                      "Target Date",
    "allocation--15% to 30% equity":    "Multi-Asset",
    "allocation--30% to 50% equity":    "Multi-Asset",
    "allocation--50% to 70% equity":    "Multi-Asset",
    "allocation--70% to 85% equity":    "Multi-Asset",
    "world large-stock blend":          "World Large Core",
    "world large-stock growth":         "World Large Growth",
    "world large-stock value":          "World Large Value",
    "world small/mid stock":            "Global SMID",
}

# yfinance sector → Sector_new__c (Salesforce picklist values)
SECTOR_MAP = {
    "Technology":              "Information Technology",
    "Healthcare":              "Health Care",
    "Financial Services":      "Financials",
    "Consumer Cyclical":       "Consumer Discretionary",
    "Consumer Defensive":      "Consumer Staples",
    "Communication Services":  "Communication Services",
    "Industrials":             "Industrials",
    "Basic Materials":         "Materials",
    "Real Estate":             "Real Estate",
    "Energy":                  "Energy",
    "Utilities":               "Utilities",
}

# ── Lookup helpers ────────────────────────────────────────────────────────────

def lookup_cusip(cusip: str) -> dict | None:
    headers = {"Content-Type": "application/json"}
    if OPENFIGI_KEY:
        headers["X-OPENFIGI-APIKEY"] = OPENFIGI_KEY
    resp = requests.post(OPENFIGI_URL, headers=headers,
                         json=[{"idType": "ID_CUSIP", "idValue": cusip}], timeout=15)
    resp.raise_for_status()
    data = resp.json()
    if not data or not data[0].get("data"):
        return None
    entries = data[0]["data"]
    primary = entries[0]
    for entry in entries:
        if entry.get("exchCode") in ("US", "UN", "UQ"):
            primary = entry
            break
    return primary


def get_yfinance_info(ticker: str) -> dict:
    try:
        return yf.Ticker(ticker).info or {}
    except Exception:
        return {}


# ── Derivation functions ──────────────────────────────────────────────────────

def derive_product_structure(figi: dict) -> str | None:
    return PRODUCT_STRUCTURE_MAP.get(figi.get("securityType", ""))


def derive_asset_class(figi: dict, yf_info: dict) -> str | None:
    name = (figi.get("name") or "").lower()
    for kw, ac in ASSET_CLASS_KEYWORDS.items():
        if kw in name:
            return ac
    security_type = figi.get("securityType", "")
    if security_type in ("ETP", "ETF", "Mutual Fund", "Closed-End Fund", "Unit Investment Trust"):
        category  = (yf_info.get("category") or "").lower()
        long_name = (yf_info.get("longName") or "").lower()
        combined  = f"{category} {long_name}"
        for kw, ac in ASSET_CLASS_KEYWORDS.items():
            if kw in combined:
                return ac
        if "bond" in category or "fixed" in category:
            return "US Fixed Income"
        if "equity" in category or "stock" in category:
            return "US Equities"
        if "real estate" in category or "reit" in category:
            return "Real Assets"
    market_sector = figi.get("marketSector", "")
    if market_sector == "Equity" and security_type == "Common Stock":
        return "US Equities"
    if market_sector == "Fixed Income":
        return "US Fixed Income"
    if market_sector == "Money Market":
        return "Cash"
    return None


def derive_sub_asset_class(yf_info: dict, figi: dict) -> str | None:
    category = (yf_info.get("category") or "").lower().strip()
    for key, val in SUB_ASSET_CLASS_MAP.items():
        if key in category:
            return val
    return None


def derive_active_passive(figi: dict, yf_info: dict) -> str | None:
    name     = (figi.get("name") or "").lower()
    category = (yf_info.get("category") or "").lower()
    combined = f"{name} {category}"
    factor_kws = {"smart beta", "factor", "quality factor", "momentum factor",
                  "low volatility", "multi-factor", "dividend growth"}
    passive_kws = {"index", " idx ", "passive", "s&p", "russell", "msci",
                   "nasdaq", "bloomberg", "ice ", "dow jones", "ftse", "stoxx"}
    for kw in factor_kws:
        if kw in combined:
            return "Factor-Based"
    for kw in passive_kws:
        if kw in combined:
            return "Passive"
    if yf_info:
        return "Active"
    return None


def derive_geography(figi: dict, yf_info: dict) -> str | None:
    name     = (figi.get("name") or "").lower()
    category = (yf_info.get("category") or "").lower()
    combined = f"{name} {category}"
    if any(k in combined for k in ("emerging market", "emerging markets")):
        return "Non-US"
    if any(k in combined for k in ("international", "foreign", "global", "world", "europe",
                                    "asia", "china", "japan", "latin", "africa")):
        return "Global"
    return "USA"


def derive_fund_type(yf_info: dict) -> str | None:
    qt = (yf_info.get("quoteType") or "").upper()
    if qt in ("ETF", "MUTUALFUND"):
        return "Open End"
    return None


def derive_sector(yf_info: dict) -> str | None:
    return SECTOR_MAP.get(yf_info.get("sector", ""))


def fmt_aum(v) -> str | None:
    if not v:
        return None
    v = int(v)
    if v >= 1_000_000_000:
        return f"${v/1_000_000_000:.2f}B"
    if v >= 1_000_000:
        return f"${v/1_000_000:.2f}M"
    return f"${v:,}"


def fmt_date(ts) -> str | None:
    if not ts:
        return None
    try:
        return datetime.utcfromtimestamp(int(ts)).strftime("%Y-%m-%d")
    except Exception:
        return None


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/lookup", methods=["POST"])
def lookup():
    cusip = request.json.get("cusip", "").strip().upper()
    if not cusip:
        return jsonify({"error": "Please enter a CUSIP."}), 400

    try:
        figi = lookup_cusip(cusip)
    except Exception as e:
        return jsonify({"error": f"OpenFIGI request failed: {e}"}), 502

    if not figi:
        return jsonify({"error": f"No data found for CUSIP {cusip}."}), 404

    ticker   = figi.get("ticker")
    yf_info  = get_yfinance_info(ticker) if ticker else {}

    return jsonify({
        # Raw FIGI data
        "cusip":          cusip,
        "name":           figi.get("name"),
        "ticker":         ticker,
        "figi":           figi.get("figi"),
        "security_type":  figi.get("securityType"),
        "security_type2": figi.get("securityType2"),
        "market_sector":  figi.get("marketSector"),
        "exchange":       figi.get("exchCode"),

        # yfinance extras
        "fund_family":    yf_info.get("fundFamily"),
        "yf_category":    yf_info.get("category"),
        "description":    yf_info.get("longBusinessSummary"),
        "aum":            fmt_aum(yf_info.get("totalAssets")),
        "inception_date": fmt_date(yf_info.get("fundInceptionDate")),

        # Derived Salesforce fields
        "product_structure": derive_product_structure(figi),
        "asset_class":       derive_asset_class(figi, yf_info),
        "sub_asset_class":   derive_sub_asset_class(yf_info, figi),
        "active_passive":    derive_active_passive(figi, yf_info),
        "geography":         derive_geography(figi, yf_info),
        "fund_type":         derive_fund_type(yf_info),
        "sector":            derive_sector(yf_info),
    })


if __name__ == "__main__":
    app.run(debug=True, port=8080)
