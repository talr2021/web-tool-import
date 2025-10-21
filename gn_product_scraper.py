#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
GoNature Product Scraper → WooCommerce CSV
------------------------------------------
Given a product URL, this script:
- Scrapes product data (title, description, price, brand, SKU) when available
- Extracts images (prefers product gallery, OpenGraph, JSON-LD) and downloads them
- Processes every image into a 1080x1080 JPEG on white background
- Detects WooCommerce/Shopify variations if present (via data-product_variations JSON, JSON-LD, or page markup)
- Outputs a WooCommerce‑ready CSV (parent product + variations) pointing to the processed images
- Creates images.zip with all processed images for easy import via WP All Import (recommended)
- Optional: If OPENAI_API_KEY is set, can enhance descriptions with a generative model (off by default)

Usage:
  python gn_product_scraper.py "https://example.com/product/xyz" \
      --brand "GoNature" --category "Backpacks" --tags "hiking,daypack" \
      --sku-prefix "GN-" --out-prefix "product_xyz"

Requires: requests, beautifulsoup4, pillow, pandas, lxml
"""
import os, re, io, csv, sys, json, math, glob, time, html
import zipfile
from urllib.parse import urljoin, urlparse
from dataclasses import dataclass, field
from typing import List, Dict, Optional, Tuple

import requests
from bs4 import BeautifulSoup
from PIL import Image, ImageOps
import pandas as pd

# ----------------------------- Helpers ---------------------------------

def slugify(text: str) -> str:
    t = re.sub(r'\s+', '-', text.strip())
    t = re.sub(r'[^A-Za-z0-9\-_]+', '', t)
    return t[:80] if t else 'item'

def fetch(url: str, headers: Optional[dict] = None) -> requests.Response:
    h = {
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/119.0 Safari/537.36"
    }
    if headers:
        h.update(headers)
    r = requests.get(url, headers=h, timeout=30)
    r.raise_for_status()
    return r

def ensure_dir(path: str):
    os.makedirs(path, exist_ok=True)

def to_1080_square_white(img: Image.Image) -> Image.Image:
    if img.mode in ("RGBA", "P"):
        img = img.convert("RGB")
    # fit inside 1080x1080 preserving aspect
    max_side = 1080
    img = ImageOps.contain(img, (max_side, max_side))
    # paste centered on white canvas
    canvas = Image.new("RGB", (1080, 1080), (255, 255, 255))
    x = (1080 - img.width) // 2
    y = (1080 - img.height) // 2
    canvas.paste(img, (x, y))
    return canvas

def save_jpeg(img: Image.Image, path: str, quality=90):
    img.save(path, format="JPEG", quality=quality, optimize=True, progressive=True)

def get_json_ld(soup: BeautifulSoup) -> List[dict]:
    data = []
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            txt = tag.string or tag.text or ""
            if txt.strip():
                parsed = json.loads(txt)
                if isinstance(parsed, dict):
                    data.append(parsed)
                elif isinstance(parsed, list):
                    data.extend(parsed)
        except Exception:
            continue
    return data

def pick_product_from_jsonld(items: List[dict]) -> Optional[dict]:
    for it in items:
        t = it.get("@type") if isinstance(it, dict) else None
        if t in ("Product", ["Product"]):
            return it
        # Nested graph
        if "@graph" in it and isinstance(it["@graph"], list):
            for node in it["@graph"]:
                if isinstance(node, dict) and node.get("@type") == "Product":
                    return node
    return None

def collect_img_candidates(soup: BeautifulSoup, base_url: str) -> List[str]:
    urls = []

    # 1) WooCommerce product gallery
    for sel in [".woocommerce-product-gallery img", ".product-images img", ".images img", ".gallery img"]:
        for img in soup.select(sel):
            src = img.get("data-src") or img.get("src") or img.get("data-large_image") or ""
            if src:
                urls.append(urljoin(base_url, src))

    # 2) OpenGraph
    for meta in soup.find_all("meta"):
        if meta.get("property") in ("og:image", "og:image:url"):
            c = meta.get("content")
            if c:
                urls.append(urljoin(base_url, c))

    # 3) JSON-LD product images
    jld = get_json_ld(soup)
    prod = pick_product_from_jsonld(jld)
    if prod:
        imgs = prod.get("image")
        if isinstance(imgs, list):
            for u in imgs:
                urls.append(urljoin(base_url, u))
        elif isinstance(imgs, str):
            urls.append(urljoin(base_url, imgs))

    # 4) Fallback: all imgs with likely product classes
    for img in soup.find_all("img"):
        src = img.get("src") or img.get("data-src") or ""
        if not src:
            continue
        cls = " ".join(img.get("class", []))
        if any(k in cls.lower() for k in ["product", "gallery", "zoom"]):
            urls.append(urljoin(base_url, src))

    # dedupe preserving order
    seen = set()
    out = []
    for u in urls:
        u = u.split("?")[0]
        if u not in seen and u.lower().startswith(("http://", "https://")):
            seen.add(u)
            out.append(u)
    return out

def parse_variations_from_wc(soup: BeautifulSoup, base_url: str) -> Tuple[Dict[str, List[str]], List[dict]]:
    """Attempts to parse WooCommerce variations from data-product_variations JSON.
    Returns: (attributes_map, variations_list). attributes_map like {"attribute_pa_color": ["Black","Blue"], ...}
    Each variation dict minimally has {"attributes": {"attribute_pa_color": "Black", ...}, "image": url, "sku": str, "price": str}
    """
    attrs_map: Dict[str, List[str]] = {}
    variations: List[dict] = []
    form = soup.find("form", {"class": re.compile(r"variations_form")})
    if not form:
        return attrs_map, variations

    data_json = form.get("data-product_variations") or form.get("data-product_variations-json")
    if data_json:
        try:
            parsed = json.loads(html.unescape(data_json))
            if isinstance(parsed, list):
                for var in parsed:
                    att = var.get("attributes") or {}
                    img = None
                    if isinstance(var.get("image"), dict):
                        img = var["image"].get("src") or var["image"].get("url")
                    elif isinstance(var.get("image"), str):
                        img = var.get("image")
                    sku = var.get("sku") or ""
                    price = var.get("display_price") or var.get("price_html") or ""
                    v = {"attributes": {}, "image": urljoin(base_url, img) if img else None, "sku": sku, "price": str(price)}
                    for k, vval in att.items():
                        if vval:
                            v["attributes"][k] = vval
                            attrs_map.setdefault(k, [])
                            if vval not in attrs_map[k]:
                                attrs_map[k].append(vval)
                    variations.append(v)
        except Exception:
            pass
    return attrs_map, variations

def parse_basic_text(soup: BeautifulSoup) -> Tuple[str, str]:
    # Title
    title = ""
    h = soup.find(["h1","h2"], {"class": re.compile(r"product")}) or soup.find("h1")
    if h and h.get_text(strip=True):
        title = h.get_text(strip=True)
    if not title:
        og = soup.find("meta", property="og:title")
        if og and og.get("content"):
            title = og["content"].strip()
    # Short/long description
    short = ""
    long = ""
    short_node = soup.select_one(".woocommerce-product-details__short-description, .short-description, .product-short-description")
    if short_node:
        short = short_node.get_text(" ", strip=True)
    desc_node = soup.select_one("#tab-description, .woocommerce-Tabs-panel--description, .product-description, #description")
    if desc_node:
        long = desc_node.get_text(" ", strip=True)
    if not long:
        ld = get_json_ld(soup)
        prod = pick_product_from_jsonld(ld)
        if prod and prod.get("description"):
            long = prod["description"]
    return title, (short or long[:300])

def ai_enhance(text_prompt: str) -> Optional[str]:
    """Optional: enhance description if OPENAI_API_KEY is present.
    Off by default. To enable: set env OPENAI_ENABLE=1 and OPENAI_API_KEY.
    """
    if os.environ.get("OPENAI_ENABLE","0") != "1":
        return None
    try:
        import openai  # Requires openai>=1.0.0
        openai.api_key = os.environ["OPENAI_API_KEY"]
        # Simple prompt; users can customize
        system = "You are a product copywriter for an outdoor gear brand. Write concise, benefit‑led Hebrew copy."
        user = text_prompt
        resp = openai.chat.completions.create(model="gpt-4o-mini", messages=[
            {"role":"system","content":system},
            {"role":"user","content":user}
        ], temperature=0.5)
        return resp.choices[0].message.content.strip()
    except Exception as e:
        return None

# --------------------------- Main pipeline ------------------------------

def build_woocommerce_rows(
    name: str,
    sku: str,
    short_desc: str,
    long_desc: str,
    gallery_filenames: List[str],
    brand: Optional[str] = None,
    categories: Optional[str] = None,
    tags: Optional[str] = None,
    attrs_map: Optional[Dict[str,List[str]]] = None,
    variations: Optional[List[dict]] = None,
) -> pd.DataFrame:
    """
    Build a minimal but robust WooCommerce CSV (UTF‑8 with BOM).
    Parent row is 'variable' if variations exist, otherwise 'simple'.
    Variation rows include attribute columns and image file names.
    """
    cols = [
        "ID","Type","SKU","Name","Published","Visibility in catalog","Short description","Description",
        "Tax status","In stock?","Stock","Backorders allowed?","Sold individually?","Allow customer reviews?",
        "Regular price","Sale price","Categories","Tags","Images",
        "Attribute 1 name","Attribute 1 value(s)","Attribute 1 visible","Attribute 1 global","Attribute 1 default",
        "Attribute 2 name","Attribute 2 value(s)","Attribute 2 visible","Attribute 2 global","Attribute 2 default",
        "Attribute 3 name","Attribute 3 value(s)","Attribute 3 visible","Attribute 3 global","Attribute 3 default",
        "Parent"
    ]
    rows = []

    has_vars = bool(variations)
    ptype = "variable" if has_vars else "simple"

    # Parent product
    parent_row = dict.fromkeys(cols, "")
    parent_row.update({
        "Type": ptype,
        "SKU": sku,
        "Name": name,
        "Published": 1,
        "Visibility in catalog": "visible",
        "Short description": short_desc,
        "Description": long_desc,
        "Tax status": "taxable",
        "In stock?": 1,
        "Stock": "",
        "Backorders allowed?": 0,
        "Sold individually?": 0,
        "Allow customer reviews?": 1,
        "Regular price": "",
        "Sale price": "",
        "Categories": categories or "",
        "Tags": tags or "",
        "Images": ", ".join(gallery_filenames) if gallery_filenames else ""
    })

    # Map attributes onto parent if variations exist
    attr_names = []
    if has_vars and attrs_map:
        for i, (attr_key, values) in enumerate(list(attrs_map.items())[:3], start=1):
            # Derive a nice attribute label
            clean_name = attr_key.replace("attribute_pa_", "").replace("attribute_", "").replace("_"," ").title()
            parent_row[f"Attribute {i} name"] = clean_name
            parent_row[f"Attribute {i} value(s)"] = "|".join(sorted(set(values)))
            parent_row[f"Attribute {i} visible"] = 1
            parent_row[f"Attribute {i} global"] = 0  # use 0 for local attributes
            parent_row[f"Attribute {i} default"] = ""
            attr_names.append((i, clean_name))
    rows.append(parent_row)

    # Variation rows
    if has_vars:
        for v in variations:
            vrow = dict.fromkeys(cols, "")
            vrow.update({
                "Type": "variation",
                "Parent": sku,
                "Published": 1,
                "In stock?": 1,
                "Visibility in catalog": "visible",
                "Allow customer reviews?": 1,
                "SKU": v.get("sku",""),
                "Regular price": v.get("price",""),
                "Images": os.path.basename(v["image"]) if v.get("image") else ""
            })
            # Map attributes into columns consistent with parent
            vattrs = v.get("attributes", {})
            i = 1
            for pk, pv in vattrs.items():
                clean_name = pk.replace("attribute_pa_", "").replace("attribute_", "").replace("_"," ").title()
                vrow[f"Attribute {i} name"] = clean_name
                vrow[f"Attribute {i} value(s)"] = pv
                vrow[f"Attribute {i} visible"] = 1
                vrow[f"Attribute {i} global"] = 0
                i += 1
            rows.append(vrow)

    df = pd.DataFrame(rows, columns=cols)
    return df

def main():
    import argparse
    ap = argparse.ArgumentParser(description="Scrape product page → WooCommerce CSV + 1080x1080 images")
    ap.add_argument("url", help="Product page URL")
    ap.add_argument("--brand", default="", help="Brand name (optional)")
    ap.add_argument("--category", default="", help="WooCommerce category path, e.g., 'Gear > Backpacks'")
    ap.add_argument("--tags", default="", help="Comma-separated tags")
    ap.add_argument("--sku-prefix", default="GN-", help="Prefix for SKU if none is detected")
    ap.add_argument("--out-prefix", default="", help="Output file prefix (defaults to slugified product title)")
    ap.add_argument("--max-images", type=int, default=12, help="Max gallery images to process (default 12)")
    ap.add_argument("--no-ai", action="store_true", help="Force disable AI description even if OPENAI env is set")
    args = ap.parse_args()

    resp = fetch(args.url)
    base_url = "{uri.scheme}://{uri.netloc}/".format(uri=urlparse(args.url))
    soup = BeautifulSoup(resp.text, "lxml")

    # Basic text
    title, short_desc_guess = parse_basic_text(soup)
    if not title:
        title = "מוצר ללא שם"
    sku = ""
    # Try common SKU nodes
    sku_node = soup.select_one(".sku, .product-sku, [itemprop=sku]")
    if sku_node and sku_node.get_text(strip=True):
        sku = sku_node.get_text(strip=True)
    if not sku:
        sku = args.sku_prefix + slugify(title)

    # Long description
    long_desc = short_desc_guess
    # Optional AI enhancement
    if os.environ.get("OPENAI_ENABLE","0") == "1" and not args.no_ai:
        prompt = f"הפוך את הטקסט הבא לתיאור מוצר שיווקי וקצר בעברית: {short_desc_guess}\n" \
                 f"לאחר מכן כתוב תיאור מלא עם מאפיינים עיקריים בנקודות עבור המוצר '{title}'."
        enhanced = ai_enhance(prompt)
        if enhanced:
            # Quick heuristic: first paragraph short, rest long
            parts = [p.strip() for p in enhanced.split("\n") if p.strip()]
            if parts:
                short_desc_guess = parts[0][:400]
                long_desc = enhanced

    # Images
    img_urls = collect_img_candidates(soup, base_url)[:args.max_images]
    out_dir = os.path.abspath(f"./out_{slugify(title)}")
    ensure_dir(out_dir)
    img_dir = os.path.join(out_dir, "images")
    ensure_dir(img_dir)

    processed_files = []
    for idx, u in enumerate(img_urls, start=1):
        try:
            r = fetch(u)
            im = Image.open(io.BytesIO(r.content))
            square = to_1080_square_white(im)
            fn = f"{slugify(os.path.basename(u)) or 'img'}-{idx:02d}.jpg"
            fpath = os.path.join(img_dir, fn)
            save_jpeg(square, fpath, quality=90)
            processed_files.append(fn)
        except Exception as e:
            # skip bad image
            continue

    # Variations
    attrs_map, variations = parse_variations_from_wc(soup, base_url)

    # If variations detected but some lack images, try to map the first gallery image
    for v in variations:
        if not v.get("image") and processed_files:
            v["image"] = processed_files[0]

    # Build CSV
    name = title.strip()
    short_desc = short_desc_guess.strip()
    # Use first 6 images as gallery by default
    gallery = processed_files[:6]
    df = build_woocommerce_rows(
        name=name,
        sku=sku,
        short_desc=short_desc,
        long_desc=long_desc,
        gallery_filenames=gallery,
        brand=args.brand or "",
        categories=args.category or "",
        tags=args.tags or "",
        attrs_map=attrs_map,
        variations=variations
    )

    # Save CSV (UTF‑8 with BOM)
    out_prefix = args.out_prefix or slugify(name)
    csv_path = os.path.join(out_dir, f"{out_prefix}.csv")
    df.to_csv(csv_path, index=False, encoding="utf-8-sig", quoting=csv.QUOTE_MINIMAL)

    # Zip images
    zip_path = os.path.join(out_dir, f"{out_prefix}_images.zip")
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for fn in processed_files:
            zf.write(os.path.join(img_dir, fn), arcname=fn)

    # Done
    print("OK")
    print("Title:", name)
    print("SKU:", sku)
    print("CSV:", os.path.abspath(csv_path))
    print("Images ZIP:", os.path.abspath(zip_path))
    print("Gallery:", ", ".join(gallery))
    if variations:
        print(f"Detected {len(variations)} variations")
    else:
        print("No variations detected")

if __name__ == "__main__":
    main()
