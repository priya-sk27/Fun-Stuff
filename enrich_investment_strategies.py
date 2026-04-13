"""
13F Investment Strategy Enrichment — Dakota Marketplace Prototype
=================================================================
Takes Investment Strategy records in Salesforce that have a CUSIP but are
missing Product Structure and/or Asset Class, looks them up via OpenFIGI,
applies a normalization map to match Dakota's Salesforce picklist values,
and writes the enriched fields back.

Usage:
    python enrich_investment_strategies.py              # dry run (no writes)
    python enrich_investment_strategies.py --write      # write to Salesforce
    python enrich_investment_strategies.py --cusip 00777X520   # test single CUSIP

Requirements:
    pip install requests simple-salesforce python-dotenv yfinance
"""

import os
import sys
import json
import time
import argparse
import requests
import yfinance as yf
from dotenv import load_dotenv
from simple_salesforce import Salesforce

load_dotenv()

# ---------------------------------------------------------------------------
# CONFIGURATION
# ---------------------------------------------------------------------------

# Salesforce connection (loaded from .env)
SF_USERNAME   = os.getenv("SF_USERNAME")
SF_PASSWORD   = os.getenv("SF_PASSWORD")
SF_TOKEN      = os.getenv("SF_SECURITY_TOKEN")
SF_DOMAIN     = os.getenv("SF_DOMAIN", "test")          # "test" = sandbox, "login" = production
SF_INSTANCE   = os.getenv("SF_INSTANCE_URL")            # e.g. dakotanetworks--testopia.sandbox

# OpenFIGI
OPENFIGI_URL  = "https://api.openfigi.com/v3/mapping"
OPENFIGI_KEY  = os.getenv("OPENFIGI_API_KEY", "")       # free key from openfigi.com; optional but raises rate limit
OPENFIGI_BATCH_SIZE = 100                               # max per request

# ---------------------------------------------------------------------------
# PICKLIST NORMALIZATION MAPS
# These values must EXACTLY match the Dakota Marketplace Salesforce picklists.
# Update these if your picklist labels change.
# ---------------------------------------------------------------------------

# OpenFIGI securityType → Dakota "Product_Structure__c" picklist value
PRODUCT_STRUCTURE_MAP = {
    "Common Stock":          "Common Stock",
    "ETP":                   "ETF",                   # Exchange Traded Product = ETF
    "ETF":                   "ETF",
    "Mutual Fund":           "Mutual Fund",
    "Closed-End Fund":       "Closed End Fund",
    "Unit Investment Trust": "Mutual Fund",
    "Preferred Stock":       "Preferred Stock",
    "ADR":                   "Common Stock",           # treat ADRs as equities
    "Corporate":             "Separate Account",       # bonds held directly
    "Government":            "Separate Account",
    "Warrant":               "Other",
    "Right":                 "Other",
    "Index":                 "Other",
}

# Dakota "Asset_Class_picklist__c" picklist values (confirmed from testopia describe)
# Primary logic: use fund category/name keywords when marketSector is misleading
ASSET_CLASS_MAP = {
    # Direct marketSector mappings for individual securities (non-fund)
    "equity_stock":           "US Equities",
    "fixed_income_bond":      "US Fixed Income",
    "money_market":           "Cash",

    # Fund/ETF category keyword overrides (applied to fund name + yfinance category)
    # Format: keyword (lowercase) → Asset Class
    "keywords": {
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
}

# ---------------------------------------------------------------------------
# OPENFIGI LOOKUP
# ---------------------------------------------------------------------------

def lookup_cusips_openfigi(cusips: list[str]) -> dict:
    """
    Batch-look up a list of CUSIPs via OpenFIGI.
    Returns a dict: {cusip: figi_result_dict or None}
    """
    headers = {"Content-Type": "application/json"}
    if OPENFIGI_KEY:
        headers["X-OPENFIGI-APIKEY"] = OPENFIGI_KEY

    results = {}
    for i in range(0, len(cusips), OPENFIGI_BATCH_SIZE):
        batch = cusips[i : i + OPENFIGI_BATCH_SIZE]
        payload = [{"idType": "ID_CUSIP", "idValue": c} for c in batch]

        response = requests.post(OPENFIGI_URL, headers=headers, json=payload, timeout=30)
        response.raise_for_status()
        data = response.json()

        for cusip, result in zip(batch, data):
            if result.get("data"):
                # Take the first (primary) listing — usually the composite/US exchange
                primary = result["data"][0]
                # Prefer composite FIGI entry if available
                for entry in result["data"]:
                    if entry.get("exchCode") in ("US", "UN", "UQ"):  # NYSE, NYSE MKT, NASDAQ
                        primary = entry
                        break
                results[cusip] = primary
            else:
                results[cusip] = None
                print(f"  [WARN] No OpenFIGI data for CUSIP {cusip}")

        # Respect rate limit: 25 req/min without key, 250/min with key
        if not OPENFIGI_KEY and len(cusips) > OPENFIGI_BATCH_SIZE:
            time.sleep(2.5)

    return results


# ---------------------------------------------------------------------------
# ASSET CLASS DERIVATION
# ---------------------------------------------------------------------------

def derive_asset_class(figi_data: dict, ticker: str | None = None) -> str | None:
    """
    Determine Asset Class from OpenFIGI data + optional yfinance fund category.
    Returns the normalized Dakota picklist value, or None if undetermined.
    """
    if not figi_data:
        return None

    security_type = figi_data.get("securityType", "")
    market_sector  = figi_data.get("marketSector", "")
    name           = (figi_data.get("name") or "").lower()

    # --- Step 1: Keyword scan on the security name ---
    keywords = ASSET_CLASS_MAP["keywords"]
    for kw, asset_class in keywords.items():
        if kw in name:
            return asset_class

    # --- Step 2: For funds/ETPs, try yfinance for fund category ---
    if security_type in ("ETP", "ETF", "Mutual Fund", "Closed-End Fund", "Unit Investment Trust"):
        if ticker:
            try:
                fund_info = yf.Ticker(ticker).info
                category = (fund_info.get("category") or "").lower()
                long_name = (fund_info.get("longName") or "").lower()
                combined = f"{category} {long_name}"
                for kw, asset_class in keywords.items():
                    if kw in combined:
                        return asset_class
                # yfinance category direct map
                if "bond" in category or "fixed" in category or "income" in category:
                    return "US Fixed Income"
                if "equity" in category or "stock" in category:
                    return "US Equities"
                if "real estate" in category or "reit" in category:
                    return "Real Assets"
            except Exception as e:
                print(f"  [WARN] yfinance lookup failed for {ticker}: {e}")

    # --- Step 3: Fall back to marketSector for plain equities ---
    if market_sector == "Equity" and security_type == "Common Stock":
        return "US Equities"
    if market_sector == "Fixed Income":
        return "US Fixed Income"
    if market_sector == "Money Market":
        return "Cash"

    return None  # unresolved — flag for manual review


def derive_product_structure(figi_data: dict) -> str | None:
    """Map OpenFIGI securityType to Dakota Product Structure picklist value."""
    if not figi_data:
        return None
    security_type = figi_data.get("securityType", "")
    return PRODUCT_STRUCTURE_MAP.get(security_type)


# ---------------------------------------------------------------------------
# SALESFORCE
# ---------------------------------------------------------------------------

def connect_salesforce() -> Salesforce:
    """Authenticate to Salesforce (sandbox or production based on SF_DOMAIN)."""
    if SF_INSTANCE:
        sf = Salesforce(
            username=SF_USERNAME,
            password=SF_PASSWORD,
            security_token=SF_TOKEN,
            instance_url=SF_INSTANCE,
            domain=SF_DOMAIN
        )
    else:
        sf = Salesforce(
            username=SF_USERNAME,
            password=SF_PASSWORD,
            security_token=SF_TOKEN,
            domain=SF_DOMAIN
        )
    print(f"Connected to Salesforce ({SF_DOMAIN})")
    return sf


def fetch_records_needing_enrichment(sf: Salesforce) -> list[dict]:
    """
    Query Investment Strategy records that have a CUSIP but are missing
    Asset Class or Product Structure.
    """
    query = """
        SELECT Id, Name, CUSIP__c, Ticker__c, Asset_Class_picklist__c, Product_Structure__c
        FROM Investment_Strategy__c
        WHERE CUSIP__c != null
          AND (Asset_Class_picklist__c = null OR Product_Structure__c = null)
        ORDER BY Name
        LIMIT 200
    """
    result = sf.query_all(query)
    records = result["records"]
    print(f"Found {len(records)} records needing enrichment")
    return records


def update_records(sf: Salesforce, updates: list[dict]) -> None:
    """Bulk-update Investment Strategy records in Salesforce."""
    if not updates:
        print("No updates to write.")
        return
    result = sf.bulk.Investment_Strategy__c.update(updates)
    success = sum(1 for r in result if r.get("success"))
    errors  = [r for r in result if not r.get("success")]
    print(f"Updated {success}/{len(updates)} records successfully")
    if errors:
        print(f"Errors ({len(errors)}):")
        for e in errors[:5]:
            print(f"  {e}")


# ---------------------------------------------------------------------------
# SINGLE-CUSIP TEST MODE
# ---------------------------------------------------------------------------

def test_single_cusip(cusip: str) -> None:
    """
    Test the enrichment logic for a single CUSIP without touching Salesforce.
    Used for prototyping and validation.
    """
    print(f"\n{'='*60}")
    print(f"TEST: CUSIP {cusip}")
    print(f"{'='*60}")

    # 1. OpenFIGI lookup
    print("\n[1] OpenFIGI lookup...")
    figi_results = lookup_cusips_openfigi([cusip])
    figi_data = figi_results.get(cusip)

    if figi_data:
        print(f"    Name:          {figi_data.get('name')}")
        print(f"    Ticker:        {figi_data.get('ticker')}")
        print(f"    securityType:  {figi_data.get('securityType')}")
        print(f"    securityType2: {figi_data.get('securityType2')}")
        print(f"    marketSector:  {figi_data.get('marketSector')}")
        print(f"    exchCode:      {figi_data.get('exchCode')}")
        print(f"    FIGI:          {figi_data.get('figi')}")
    else:
        print("    No data returned.")
        return

    ticker = figi_data.get("ticker")

    # 2. Derive Product Structure
    product_structure = derive_product_structure(figi_data)
    print(f"\n[2] Derived Product Structure: {product_structure!r}")

    # 3. Derive Asset Class
    print(f"\n[3] Deriving Asset Class (with yfinance fallback for funds)...")
    asset_class = derive_asset_class(figi_data, ticker)
    print(f"    Derived Asset Class:         {asset_class!r}")

    # 4. Summary
    print(f"\n{'─'*60}")
    print(f"  CUSIP:             {cusip}")
    print(f"  Identified as:     {figi_data.get('name')} ({ticker})")
    print(f"  Product Structure: {product_structure}  →  write to Product_Structure__c")
    print(f"  Asset Class:       {asset_class}  →  write to Asset_Class__c")

    if product_structure is None or asset_class is None:
        print(f"\n  ⚠️  One or more fields could not be resolved — would flag for manual review")
    else:
        print(f"\n  ✅  Both fields resolved — ready to write to Salesforce")
    print(f"{'='*60}\n")


# ---------------------------------------------------------------------------
# MAIN ENRICHMENT PIPELINE
# ---------------------------------------------------------------------------

def run_enrichment(write_to_sf: bool = False) -> None:
    """
    Full enrichment pipeline:
    1. Connect to Salesforce
    2. Fetch records missing Asset Class or Product Structure
    3. Batch-lookup CUSIPs via OpenFIGI
    4. Derive normalized field values
    5. Print results (dry run) or write back to Salesforce
    """
    sf = connect_salesforce()
    records = fetch_records_needing_enrichment(sf)

    if not records:
        print("Nothing to enrich — all records already have Asset Class and Product Structure.")
        return

    # Extract CUSIPs for batch lookup
    cusips = [r["CUSIP__c"] for r in records if r.get("CUSIP__c")]
    print(f"\nLooking up {len(cusips)} CUSIPs via OpenFIGI...")
    figi_map = lookup_cusips_openfigi(cusips)

    updates = []
    unresolved = []

    for record in records:
        cusip  = record.get("CUSIP__c")
        ticker = record.get("Ticker__c")
        name   = record.get("Name")
        rec_id = record.get("Id")

        figi_data         = figi_map.get(cusip)
        product_structure = derive_product_structure(figi_data)
        asset_class       = derive_asset_class(figi_data, ticker)

        print(f"\n  {name} ({cusip})")
        print(f"    Product Structure: {record.get('Product_Structure__c') or '(empty)'!r}  →  {product_structure!r}")
        print(f"    Asset Class:       {record.get('Asset_Class_picklist__c') or '(empty)'!r}  →  {asset_class!r}")

        update = {"Id": rec_id}
        if product_structure and not record.get("Product_Structure__c"):
            update["Product_Structure__c"] = product_structure
        if asset_class and not record.get("Asset_Class_picklist__c"):
            update["Asset_Class_picklist__c"] = asset_class

        # Flag unresolved records for manual review
        if product_structure is None or asset_class is None:
            unresolved.append(name)

        if len(update) > 1:   # more than just Id
            updates.append(update)

    print(f"\n{'─'*60}")
    print(f"Records to update:   {len(updates)}")
    print(f"Unresolved (flagged): {len(unresolved)}")
    if unresolved:
        print("  Flagged for review:", ", ".join(unresolved[:10]))

    if write_to_sf:
        print("\nWriting to Salesforce...")
        update_records(sf, updates)
    else:
        print("\n[DRY RUN] No changes written. Run with --write to apply updates.")


# ---------------------------------------------------------------------------
# ENTRY POINT
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Enrich 13F Investment Strategy records in Salesforce")
    parser.add_argument("--write",  action="store_true", help="Write enriched values to Salesforce (default: dry run)")
    parser.add_argument("--cusip",  type=str,            help="Test enrichment for a single CUSIP without Salesforce")
    args = parser.parse_args()

    if args.cusip:
        test_single_cusip(args.cusip)
    else:
        run_enrichment(write_to_sf=args.write)
