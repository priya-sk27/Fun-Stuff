"""
13F CUSIP Lookup Web App
Accepts a CUSIP, looks it up via OpenFIGI, and displays enriched
Investment Strategy fields — no Salesforce writes required.
"""

import os
import requests
from flask import Flask, render_template, request, jsonify
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)

OPENFIGI_URL = "https://api.openfigi.com/v3/mapping"
OPENFIGI_KEY = os.getenv("OPENFIGI_API_KEY", "")

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
    "fixed income":       "US Fixed Income",
    "fixed rate":         "US Fixed Income",
    "bond":               "US Fixed Income",
    "bonds":              "US Fixed Income",
    "treasury":           "US Fixed Income",
    "credit":             "US Fixed Income",
    "mbs":                "US Fixed Income",
    "mortgage":           "US Fixed Income",
    "aggregate":          "US Fixed Income",
    "corporate bond":     "US Fixed Income",
    "municipal":          "US Fixed Income",
    "muni":               "US Fixed Income",
    "government":         "US Fixed Income",
    "high yield":         "US Fixed Income",
    "inflation":          "US Fixed Income",
    "tips":               "US Fixed Income",
    "real estate":        "Real Assets",
    "reit":               "Real Assets",
    "infrastructure":     "Real Assets",
    "commodity":          "Real Assets",
    "commodities":        "Real Assets",
    "energy":             "Real Assets",
    "natural resources":  "Real Assets",
    "gold":               "Real Assets",
    "private equity":     "Private Equity",
    "buyout":             "Private Equity",
    "venture":            "Venture Capital",
    "hedge":              "Hedge Funds/Liquid Alternatives",
    "absolute return":    "Hedge Funds/Liquid Alternatives",
    "multi-asset":        "Multi-Asset",
    "balanced":           "Multi-Asset",
    "allocation":         "Multi-Asset",
    "emerging market":    "Emerging Market Equities",
    "emerging markets":   "Emerging Market Equities",
    "international":      "International Equities",
    "global":             "Global Equities",
    "world":              "Global Equities",
    "europe":             "Global Equities",
    "asia":               "Global Equities",
    "china":              "Global Equities",
    "japan":              "Global Equities",
}


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


def derive_product_structure(figi: dict) -> str | None:
    return PRODUCT_STRUCTURE_MAP.get(figi.get("securityType", ""))


def derive_asset_class(figi: dict) -> str | None:
    name = (figi.get("name") or "").lower()
    for kw, asset_class in ASSET_CLASS_KEYWORDS.items():
        if kw in name:
            return asset_class

    security_type = figi.get("securityType", "")
    market_sector  = figi.get("marketSector", "")

    if market_sector == "Equity" and security_type == "Common Stock":
        return "US Equities"
    if market_sector == "Fixed Income":
        return "US Fixed Income"
    if market_sector == "Money Market":
        return "Cash"
    return None


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

    return jsonify({
        "cusip":             cusip,
        "name":              figi.get("name"),
        "ticker":            figi.get("ticker"),
        "figi":              figi.get("figi"),
        "security_type":     figi.get("securityType"),
        "security_type2":    figi.get("securityType2"),
        "market_sector":     figi.get("marketSector"),
        "exchange":          figi.get("exchCode"),
        "product_structure": derive_product_structure(figi),
        "asset_class":       derive_asset_class(figi),
    })


if __name__ == "__main__":
    app.run(debug=True, port=5000)
