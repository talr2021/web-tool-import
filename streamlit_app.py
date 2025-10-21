# -*- coding: utf-8 -*-
import os, io, re, json, zipfile, textwrap
from urllib.parse import urlparse
import streamlit as st

# Networking / parsing / imaging
import requests
from bs4 import BeautifulSoup
from PIL import Image, ImageOps
import pandas as pd
import csv
import html

# ----------------------------- Helpers (from scraper) ---------------------------------

def slugify(text: str) -> str:
    import re
    t = re.sub(r'\s+', '-', text.strip())
    t = re.sub(r'[^A-Za-z0-9\-_]+', '', t)
    return t[:80] if t else 'item'

def fetch(url: str, headers: dict | None = None) -> requests.Response:
    h = {
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/119.0 Safari/537.36"
    }
    if headers:
        h.update(headers)
    r = requests.get(url, headers=h, timeout=30)
    r.raise_for_status()
    return r

def to_1080_square_white(img: Image.Image) -> Image.Image:
    if img.mode in ("RGBA", "P"):
        img = img.convert("RGB")
    max_side = 1080
    img = ImageOps.contain(img, (max_side, max_side))
    canvas = Image.new("RGB", (1080, 1080), (255, 255, 255))
    x = (1080 - img.width) // 2
    y = (1080 - img.height) // 2
    canvas.paste(img, (x, y))
    return canvas

def save_jpeg(img: Image.Image, path: str, quality=90):
    img.save(path, format="JPEG", quality=quality, optimize=True, progressive=True)

def get_json_ld(soup: BeautifulSoup) -> list[dict]:
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

def pick_product_from_jsonld(items: list[dict]) -> dict | None:
    for it in items:
        t = it.get("@type") if isinstance(it, dict) else None
        if t in ("Product", ["Product"]):
            return it
        if "@graph" in it and isinstance(it["@graph"], list):
            for node in it["@graph"]:
                if isinstance(node, dict) and node.get("@type") == "Product":
                    return node
    return None

def collect_img_candidates(soup: BeautifulSoup, base_url: str) -> list[str]:
    urls = []

    for sel in [".woocommerce-product-gallery img", ".product-images img", ".images img", ".gallery img"]:
        for img in soup.select(sel):
            src = img.get("data-src") or img.get("src") or img.get("data-large_image") or ""
            if src:
                urls.append(requests.compat.urljoin(base_url, src))

    for meta in soup.find_all("meta"):
        if meta.get("property") in ("og:image", "og:image:url"):
            c = meta.get("content")
            if c:
                urls.append(requests.compat.urljoin(base_url, c))

    jld = get_json_ld(soup)
    prod = pick_product_from_jsonld(jld)
    if prod:
        imgs = prod.get("image")
        if isinstance(imgs, list):
            for u in imgs:
                urls.append(requests.compat.urljoin(base_url, u))
        elif isinstance(imgs, str):
            urls.append(requests.compat.urljoin(base_url, imgs))

    for img in soup.find_all("img"):
        src = img.get("src") or img.get("data-src") or ""
        if not src:
            continue
        cls = " ".join(img.get("class", []))
        if any(k in cls.lower() for k in ["product", "gallery", "zoom"]):
            urls.append(requests.compat.urljoin(base_url, src))

    seen = set(); out = []
    for u in urls:
        u = u.split("?")[0]
        if u not in seen and u.lower().startswith(("http://", "https://")):
            seen.add(u); out.append(u)
    return out

def parse_variations_from_wc(soup: BeautifulSoup, base_url: str):
    attrs_map = {}
    variations = []
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
                    v = {"attributes": {}, "image": requests.compat.urljoin(base_url, img) if img else None, "sku": sku, "price": str(price)}
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

def parse_basic_text(soup: BeautifulSoup):
    title = ""
    h = soup.find(["h1","h2"], {"class": re.compile(r"product")}) or soup.find("h1")
    if h and h.get_text(strip=True):
        title = h.get_text(strip=True)
    if not title:
        og = soup.find("meta", property="og:title")
        if og and og.get("content"):
            title = og["content"].strip()
    short = ""; longd = ""
    short_node = soup.select_one(".woocommerce-product-details__short-description, .short-description, .product-short-description")
    if short_node:
        short = short_node.get_text(" ", strip=True)
    desc_node = soup.select_one("#tab-description, .woocommerce-Tabs-panel--description, .product-description, #description")
    if desc_node:
        longd = desc_node.get_text(" ", strip=True)
    if not longd:
        ld = get_json_ld(soup)
        prod = pick_product_from_jsonld(ld)
        if prod and prod.get("description"):
            longd = prod["description"]
    return title, (short or longd[:300]), (longd or short)

def build_woocommerce_rows(name, sku, short_desc, long_desc, gallery_filenames, categories="", tags="", attrs_map=None, variations=None):
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
        "Categories": categories or "",
        "Tags": tags or "",
        "Images": ", ".join(gallery_filenames) if gallery_filenames else ""
    })
    rows.append(parent_row)

    if has_vars and attrs_map:
        i_map = []
        for i, (attr_key, values) in enumerate(list(attrs_map.items())[:3], start=1):
            clean_name = attr_key.replace("attribute_pa_", "").replace("attribute_", "").replace("_"," ").title()
            parent_row[f"Attribute {i} name"] = clean_name
            parent_row[f"Attribute {i} value(s)"] = "|".join(sorted(set(values)))
            parent_row[f"Attribute {i} visible"] = 1
            parent_row[f"Attribute {i} global"] = 0
            parent_row[f"Attribute {i} default"] = ""
            i_map.append((i, clean_name))

        for v in variations:
            vrow = dict.fromkeys(cols, "")
            vrow.update({
                "Type": "variation",
                "Parent": parent_row["SKU"],
                "Published": 1,
                "In stock?": 1,
                "Visibility in catalog": "visible",
                "Allow customer reviews?": 1,
                "SKU": v.get("sku",""),
                "Regular price": v.get("price",""),
                "Images": os.path.basename(v["image"]) if v.get("image") else ""
            })
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

    return pd.DataFrame(rows, columns=cols)

def process_single_url(url, brand="", category="", tags="", sku_prefix="GN-", max_images=12, openai_enable=False):
    r = fetch(url)
    base_url = "{uri.scheme}://{uri.netloc}/".format(uri=urlparse(url))
    soup = BeautifulSoup(r.text, "lxml")

    title, short_guess, long_guess = parse_basic_text(soup)
    if not title:
        title = "מוצר ללא שם"
    sku = ""
    sku_node = soup.select_one(".sku, .product-sku, [itemprop=sku]")
    if sku_node and sku_node.get_text(strip=True):
        sku = sku_node.get_text(strip=True)
    if not sku:
        sku = sku_prefix + slugify(title)

    long_desc = long_guess
    short_desc = short_guess[:400]

    img_urls = collect_img_candidates(soup, base_url)[:max_images]
    out_dir = os.path.abspath(f"./out_{slugify(title)}")
    os.makedirs(out_dir, exist_ok=True)
    img_dir = os.path.join(out_dir, "images")
    os.makedirs(img_dir, exist_ok=True)

    processed_files = []
    for idx, u in enumerate(img_urls, start=1):
        try:
            rr = fetch(u)
            im = Image.open(io.BytesIO(rr.content))
            square = to_1080_square_white(im)
            fn = f"{slugify(os.path.basename(u)) or 'img'}-{idx:02d}.jpg"
            fpath = os.path.join(img_dir, fn)
            save_jpeg(square, fpath, quality=90)
            processed_files.append(fn)
        except Exception:
            continue

    attrs_map, variations = parse_variations_from_wc(soup, base_url)
    for v in variations:
        if not v.get("image") and processed_files:
            v["image"] = processed_files[0]

    gallery = processed_files[:6]
    df = build_woocommerce_rows(
        name=title.strip(),
        sku=sku,
        short_desc=short_desc.strip(),
        long_desc=long_desc.strip(),
        gallery_filenames=gallery,
        categories=category or "",
        tags=tags or "",
        attrs_map=attrs_map,
        variations=variations
    )

    csv_path = os.path.join(out_dir, f"{slugify(title)}.csv")
    df.to_csv(csv_path, index=False, encoding="utf-8-sig", quoting=csv.QUOTE_MINIMAL)

    zip_path = os.path.join(out_dir, f"{slugify(title)}_images.zip")
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for fn in processed_files:
            zf.write(os.path.join(img_dir, fn), arcname=fn)

    return {
        "title": title,
        "sku": sku,
        "csv_path": os.path.abspath(csv_path),
        "zip_path": os.path.abspath(zip_path),
        "out_dir": os.path.abspath(out_dir),
        "gallery": gallery,
        "variations_count": len(variations),
        "images_count": len(processed_files)
    }

# --------------------------------- UI ------------------------------------

st.set_page_config(page_title="GoNature → WooCommerce Builder", layout="wide")
st.title("כלי העלאה אוטומטי: URL → תמונות 1080×1080 + CSV ל-WooCommerce")

with st.sidebar:
    st.header("הגדרות כלליות")
    brand = st.text_input("מותג (לא חובה)", value="GoNature")
    category = st.text_input("קטגוריה (היררכיה עם > )", value="")
    tags = st.text_input("תגים (מופרדים בפסיקים)", value="")
    sku_prefix = st.text_input("קידומת SKU", value="GN-")
    max_images = st.number_input("מס׳ מקסימלי לתמונות (גלריה)", min_value=1, max_value=30, value=12, step=1)

st.subheader("הדביקו לינקים (אחד בכל שורה)")
urls_text = st.text_area("לינקים", height=160, placeholder="https://example.com/product/1\nhttps://example.com/product/2")
run = st.button("הפעל")

if run:
    urls = [u.strip() for u in urls_text.splitlines() if u.strip()]
    if not urls:
        st.warning("לא הוזנו לינקים.")
    else:
        results = []
        pb = st.progress(0)
        for i, url in enumerate(urls, start=1):
            try:
                st.write(f"🔎 מעבד: {url}")
                res = process_single_url(
                    url=url,
                    brand=brand,
                    category=category,
                    tags=tags,
                    sku_prefix=sku_prefix,
                    max_images=int(max_images),
                    openai_enable=False
                )
                results.append(res)
                st.success(f"✓ {res['title']} — וריאציות: {res['variations_count']} | תמונות: {res['images_count']}")
                st.write("CSV:", res["csv_path"])
                st.write("ZIP:", res["zip_path"])
                with open(res["csv_path"], "rb") as f:
                    st.download_button(label=f"הורד CSV — {res['title']}",
                                       data=f, file_name=os.path.basename(res["csv_path"]))
                with open(res["zip_path"], "rb") as f:
                    st.download_button(label=f"הורד ZIP תמונות — {res['title']}",
                                       data=f, file_name=os.path.basename(res["zip_path"]))
            except Exception as e:
                st.error(f"שגיאה בעיבוד {url}: {e}")
            pb.progress(i/len(urls))

        if results:
            # יצירת ZIP מרכזי של כל התוצרים
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
                for r in results:
                    # הוסף CSV ו-ZIP תמונות
                    z.write(r["csv_path"], arcname=f"{os.path.basename(r['out_dir'])}/{os.path.basename(r['csv_path'])}")
                    z.write(r["zip_path"], arcname=f"{os.path.basename(r['out_dir'])}/{os.path.basename(r['zip_path'])}")
            buf.seek(0)
            st.download_button("הורד הכל כ-ZIP אחד", data=buf, file_name="export_bundle.zip")

st.info("טיפ: לייבוא קל מומלץ להשתמש בתוסף WP All Import, למפות את עמודת Images ולצרף את קבצי ה-ZIP של התמונות.")
