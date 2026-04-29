"""
E-Craft Design Tracker - AI Server
====================================
To start: double-click start.bat  (or run: python server.py)
Then open your browser to: http://localhost:5000
"""

import json
import os
import base64
import urllib.request
from flask import Flask, request, jsonify, Response
from flask_cors import CORS
import anthropic

# API key always comes from environment on Railway -- never from a file
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

if not ANTHROPIC_API_KEY:
    print("\n  No API key found! Set ANTHROPIC_API_KEY in Railway environment variables.\n")

app = Flask(__name__, static_folder=".")
CORS(app)  # Allow requests from GoDaddy-hosted front end

TIER_NAMES = {1: "Builder Elevated", 2: "Signature E-Craft", 3: "High Design Custom"}

BRAND_CONTEXT = {
    "tile":        "Zia Tile, Traditions in Tile, Ann Sacks, Claybrook, Walker Zanger, Waterworks Tile, Jeffrey Court, Fireclay Tile",
    "lighting":    "Visual Comfort, Flambeaux Lighting, Shades of Light, Circa Lighting, Apparatus Studio, Roll & Hill, Hudson Valley Lighting, Troy Lighting",
    "plumbing":    "Brizo, Waterworks, Hansgrohe, Newport Brass, Lefroy Brooks, Dornbracht, Kohler Artifacts, Rohl",
    "cabinets":    "Kith Cabinetry, Wolf Home Products, Plain English, deVOL Kitchens, Omega Cabinetry, Showplace",
    "wallpaper":   "Ronald Redding Designs, Katie Kime, Cole & Son, Serena & Lily, Borastapeter, De Gournay, Schumacher, Phillip Jeffries",
    "paint":       "Farrow & Ball, Benjamin Moore, Sherwin-Williams, Clare Paint, Little Greene",
    "hardware":    "Emtek, Rejuvenation, Rocky Mountain Hardware, Grandeur, Baldwin, Nostalgic Warehouse, Ashley Norton",
    "mirrors":     "Visual Comfort, Restoration Hardware, Uttermost, Currey & Company, Arteriors, Made Goods",
    "countertops": "MSI Surfaces, Arizona Tile, Walker Zanger, Cambria, Caesarstone, Silestone, Calacatta Gold Marble, Quartzmaster",
    "doors":       "TruStile Doors, Jeld-Wen, Masonite, Simpson Door, Woodgrain, Benchmark by Therma-Tru, Coastal Millwork",
    "windows":     "Andersen Windows, Pella, Marvin, Kolbe Windows, Sierra Pacific, Andersen A-Series, Windsor Windows",
    "trim":        "WindsorONE, Metrie, Alexandria Moulding, House of Antique Hardware, Ornamental Mouldings, Fypon, Pacific Coast Mouldings",
    "appliances":  "Wolf, Sub-Zero, Miele, Thermador, Viking, Monogram, Fisher & Paykel, Gaggenau, La Cornue, SMEG",
}


def detect_image_media_type(content_type_header="", url="", raw_bytes=b""):
    ct = content_type_header.split(";")[0].strip().lower()
    if ct in ("image/jpeg", "image/png", "image/webp", "image/gif"):
        return ct
    url_lower = url.lower().split("?")[0]
    if url_lower.endswith(".png"):
        return "image/png"
    if url_lower.endswith((".jpg", ".jpeg")):
        return "image/jpeg"
    if url_lower.endswith(".webp"):
        return "image/webp"
    if url_lower.endswith(".gif"):
        return "image/gif"
    if raw_bytes[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if raw_bytes[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if raw_bytes[:4] == b"RIFF" and raw_bytes[8:12] == b"WEBP":
        return "image/webp"
    if raw_bytes[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    return "image/jpeg"


def strip_fences(text):
    if "```" not in text:
        return text
    for part in text.split("```"):
        part = part.strip()
        if part.startswith("json"):
            part = part[4:].strip()
        if part.startswith("[") or part.startswith("{"):
            return part
    return text


def build_lookbook_prompt(tier_name, style_notes, cats, extra=""):
    cats_str = ", ".join(cats)
    lines = [
        "You are an expert interior design assistant for E-Craft, a luxury residential design firm.",
        "",
        "Project context:",
        f"- Design tier: {tier_name}",
        f"- Style notes: {style_notes or 'none provided'}",
    ]
    if extra:
        lines.append(extra)
    lines += [
        "",
        "IMPORTANT INSTRUCTIONS:",
        "1. Look for product images in CIRCLES or rounded frames = SELECTED items.",
        "2. EMPTY circles or placeholders = TBD (still to be determined).",
        f"3. Identify category from: {cats_str}",
        "4. Extract: brand, product line, model, finish, color, room.",
        "",
        "Return ONLY a valid JSON array, no markdown, no explanation.",
        "Each item must have exactly these keys:",
        '  "name"     - specific product name or descriptive name',
        '  "brand"    - brand/manufacturer if visible, else ""',
        f'  "category" - one of: {cats_str}',
        '  "room"     - room/location if labeled, else ""',
        '  "status"   - "selected" if circle has photo, "tbd" if empty',
        '  "detail"   - one-line description of what you see',
        '  "finish"   - finish if visible, else ""',
        '  "color"    - color name if relevant, else ""',
        '  "notes"    - any captions or labels, else ""',
        "",
        "If no design products found, return: []",
    ]
    return "\n".join(lines)


@app.route("/")
def index():
    html_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "design-tracker-v3.html")
    with open(html_path, "r", encoding="utf-8") as f:
        html = f.read()
    inject = '<script>window._AI_ONLINE = ' + str(bool(ANTHROPIC_API_KEY)).lower() + ';</script>'
    html = html.replace("</head>", inject + "\n</head>", 1)
    return Response(html, mimetype="text/html", headers={"Cache-Control": "no-store"})


@app.route("/api/health")
def health():
    return jsonify({"status": "ok", "ai": bool(ANTHROPIC_API_KEY)})


@app.route("/api/suggest", methods=["POST"])
def suggest():
    if not ANTHROPIC_API_KEY:
        return jsonify({"error": "No API key configured"}), 503

    data        = request.get_json(force=True)
    tier        = int(data.get("tier", 2))
    category    = data.get("category", "")
    field       = data.get("field", "")
    room        = data.get("room", "")
    style_notes = data.get("style_notes", "")
    row_ctx     = data.get("row_context", {})
    proj_ctx    = data.get("project_context", "")
    exclude     = data.get("exclude", [])

    tier_name = TIER_NAMES.get(tier, "Signature E-Craft")
    brands    = BRAND_CONTEXT.get(category, "leading luxury brands")

    ctx_parts = [f"{k}: {v}" for k, v in row_ctx.items()
                 if v and k not in ("id", "status", "total", "boxes", "dollar_box", "rolls")]
    ctx_str = ", ".join(ctx_parts) if ctx_parts else "no additional context"

    exclude_str = ""
    if exclude:
        exclude_str = "Do NOT suggest any of these (already shown): " + ", ".join(exclude) + "\n"

    proj_ctx_block = ""
    if proj_ctx:
        proj_ctx_block = (
            "\nIMPORTANT - Design cohesion: Your suggestions MUST visually and stylistically "
            "complement the selections already confirmed for this project. Match the dominant finish "
            "family, aesthetic (e.g. warm transitional, modern farmhouse, organic modern), and quality "
            "tier already established. Do not suggest items that clash in finish or style.\n"
            + proj_ctx
        )

    prompt_lines = [
        "You are a luxury interior design assistant for E-Craft, a high-end residential design firm in the US.",
        "",
        "Project details:",
        f"- Tier: {tier_name}",
        f"- Category: {category.title()}",
        f"- Room/Location: {room or 'not specified'}",
        f"- Field to suggest: {field}",
        f"- Style notes from designer: {style_notes or 'none provided'}",
        f"- Other details already filled in this row: {ctx_str}",
        proj_ctx_block,
        exclude_str,
        f'Suggest exactly 3 specific products for the "{field}" field.',
        f"Suggestions must be appropriate for the {tier_name} tier and the {room or category} context.",
        f"Use real, currently available products. Preferred brands for {category}: {brands}",
        "",
        "Return ONLY a valid JSON array - no markdown, no explanation, just the array.",
        "Each item must have exactly these keys:",
        '  "name"         - specific product name (brand + collection/model)',
        '  "detail"       - one short phrase: maker + key attribute',
        '  "price"        - price range as string, or "" for paint',
        '  "vendor"       - where E-Craft should order from',
        '  "brand_domain" - manufacturer website domain without www',
        '  "search_query" - optimized Google Images search string for this exact product',
    ]
    prompt = "\n".join(prompt_lines)

    try:
        client   = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=800,
            messages=[{"role": "user", "content": prompt}],
        )
        text        = strip_fences(response.content[0].text.strip())
        suggestions = json.loads(text)
        if not isinstance(suggestions, list):
            raise ValueError("Expected a list")
        return jsonify({"suggestions": suggestions[:3]})
    except json.JSONDecodeError as e:
        print(f"Suggest JSON error: {e}")
        return jsonify({"error": "Could not parse AI response"}), 500
    except Exception as e:
        print(f"Suggest error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/price-source", methods=["POST"])
def price_source():
    if not ANTHROPIC_API_KEY:
        return jsonify({"error": "No API key configured"}), 503

    data        = request.get_json(force=True)
    product     = data.get("product", "").strip()
    brand       = data.get("brand", "").strip()
    category    = data.get("category", "")
    room        = data.get("room", "")
    finish      = data.get("finish", "")
    tier        = int(data.get("tier", 2))
    mode        = data.get("mode", "exact")
    style_notes = data.get("style_notes", "")

    if not product:
        return jsonify({"error": "No product specified"}), 400

    tier_name = TIER_NAMES.get(tier, "Signature E-Craft")

    if mode == "exact":
        mode_instruction = (
            "Find the EXACT product listed (same brand, model, finish) at the lowest price available. "
            "Search across: manufacturer direct, authorized dealers, trade sources (Build.com, Ferguson, "
            "Houzz, Capitol Lighting, etc.), big-box (Home Depot Pro, Lowes Pro), and Amazon. "
            "Rank results cheapest first. Include the best 3-4 sources."
        )
        result_name_note = "Use the exact product name -- do NOT substitute alternatives."
    else:
        mode_instruction = (
            "Find 3-4 ALTERNATIVE products that are similar in style, quality, and function but at a "
            "lower price point. Match the aesthetic (finish family, style, material) closely. "
            "Each alternative should be a real, currently available product from a reputable brand. "
            "Rank by best value (quality per dollar)."
        )
        result_name_note = (
            "Each result should be a distinct alternative product name, NOT the original. "
            "Clearly state the brand and model so it can be ordered."
        )

    finish_str = f" in {finish}" if finish else ""
    prompt_lines = [
        "You are a luxury interior design procurement specialist for E-Craft, a residential design firm.",
        "Your job is to find the best sourcing options with accurate pricing and lead time estimates.",
        "",
        f"Product to source: {product}{finish_str}",
        f"Brand: {brand or 'see product name'}",
        f"Category: {category.title()}",
        f"Room: {room or 'not specified'}",
        f"Project tier: {tier_name}",
        f"Style notes: {style_notes or 'none'}",
        "",
        f"Search mode: {'EXACT MATCH' if mode=='exact' else 'BEST VALUE ALTERNATIVES'}",
        mode_instruction,
        "",
        "For each result, provide realistic lead time estimates.",
        "",
        "Return ONLY a valid JSON array. Each item must have exactly these keys:",
        '  "name"       - product name (brand + model + finish)',
        '  "vendor"     - specific vendor/retailer name',
        '  "price"      - price or price range as string',
        '  "lead_time"  - realistic shipping/lead time estimate',
        '  "detail"     - one short phrase about this source/option',
        '  "shop_url"   - direct URL to product page if known, else ""',
        '  "google_url" - Google Shopping URL',
        result_name_note,
        "No markdown, no explanation -- only the JSON array.",
    ]
    prompt = "\n".join(prompt_lines)

    try:
        client   = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1200,
            messages=[{"role": "user", "content": prompt}],
        )
        text    = strip_fences(response.content[0].text.strip())
        results = json.loads(text)
        if not isinstance(results, list):
            raise ValueError("Expected a list")
        import urllib.parse
        for r in results:
            if not r.get("google_url") and r.get("name"):
                q = urllib.parse.quote_plus(r["name"] + " buy")
                r["google_url"] = f"https://www.google.com/search?q={q}&tbm=shop"
        return jsonify({"results": results[:4]})
    except json.JSONDecodeError as e:
        print(f"Price-source JSON error: {e}")
        return jsonify({"error": "Could not parse AI response"}), 500
    except Exception as e:
        print(f"Price-source error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/analyze-lookbook", methods=["POST"])
def analyze_lookbook():
    if not ANTHROPIC_API_KEY:
        return jsonify({"error": "No API key configured"}), 503

    data        = request.get_json(force=True)
    files       = data.get("files", [])
    style_notes = data.get("style_notes", "")
    tier        = int(data.get("tier", 2))

    if not files:
        return jsonify({"error": "No files provided"}), 400

    content = []
    for f in files[:5]:
        media_type = f.get("mediaType", "image/jpeg")
        b64        = f.get("data", "")
        if not b64:
            continue
        if media_type == "application/pdf":
            content.append({"type": "document",
                            "source": {"type": "base64", "media_type": "application/pdf", "data": b64}})
        else:
            content.append({"type": "image",
                            "source": {"type": "base64", "media_type": media_type, "data": b64}})

    if not content:
        return jsonify({"error": "No valid file content"}), 400

    tier_name = TIER_NAMES.get(tier, "Signature E-Craft")
    prompt    = build_lookbook_prompt(tier_name, style_notes, list(BRAND_CONTEXT.keys()))
    content.append({"type": "text", "text": prompt})

    try:
        client   = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=2000,
            messages=[{"role": "user", "content": content}],
        )
        text  = strip_fences(response.content[0].text.strip())
        items = json.loads(text)
        if not isinstance(items, list):
            raise ValueError("Expected a list")
        return jsonify({"items": items})
    except json.JSONDecodeError as e:
        print(f"Lookbook JSON error: {e}")
        return jsonify({"error": "Could not parse AI response"}), 500
    except Exception as e:
        print(f"Lookbook error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/analyze-lookbook-url", methods=["POST"])
def analyze_lookbook_url():
    if not ANTHROPIC_API_KEY:
        return jsonify({"error": "No API key configured"}), 503

    data        = request.get_json(force=True)
    url         = data.get("url", "").strip()
    style_notes = data.get("style_notes", "")
    tier        = int(data.get("tier", 2))

    if not url:
        return jsonify({"error": "No URL provided"}), 400

    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        })
        with urllib.request.urlopen(req, timeout=15) as resp:
            content_type = resp.headers.get("Content-Type", "")
            raw = resp.read()
    except Exception as fetch_err:
        return jsonify({"error": f"Could not fetch URL: {fetch_err}"}), 400

    content   = []
    tier_name = TIER_NAMES.get(tier, "Signature E-Craft")
    cats      = list(BRAND_CONTEXT.keys())

    if "image/" in content_type or url.lower().split("?")[0].endswith(('.jpg', '.jpeg', '.png', '.webp', '.gif')):
        media_type = detect_image_media_type(content_type, url, raw)
        content.append({"type": "image",
                        "source": {"type": "base64", "media_type": media_type,
                                   "data": base64.b64encode(raw).decode()}})

    elif "pdf" in content_type or url.lower().endswith('.pdf'):
        content.append({"type": "document",
                        "source": {"type": "base64", "media_type": "application/pdf",
                                   "data": base64.b64encode(raw).decode()}})
    else:
        from html.parser import HTMLParser

        class ImgExtractor(HTMLParser):
            def __init__(self):
                super().__init__()
                self.images = []
                self.og_image = None

            def handle_starttag(self, tag, attrs):
                attrs = dict(attrs)
                if tag == "meta" and attrs.get("property") == "og:image":
                    self.og_image = attrs.get("content", "")
                if tag == "img":
                    src = attrs.get("src", "")
                    if src.startswith("http") and any(
                            src.lower().endswith(x) for x in ('.jpg', '.jpeg', '.png', '.webp')):
                        self.images.append(src)

        parser = ImgExtractor()
        try:
            parser.feed(raw.decode("utf-8", "ignore"))
        except Exception:
            pass

        image_urls = []
        if parser.og_image:
            image_urls.append(parser.og_image)
        image_urls.extend(parser.images[:4])

        if not image_urls:
            return jsonify({"error":
                "No images found at this URL. For Canva look books, export as PDF "
                "(File > Download > PDF Standard) then upload directly."}), 400

        for img_url in image_urls[:3]:
            try:
                img_req = urllib.request.Request(img_url, headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(img_req, timeout=10) as img_resp:
                    img_ct  = img_resp.headers.get("Content-Type", "")
                    img_raw = img_resp.read()
                img_media = detect_image_media_type(img_ct, img_url, img_raw)
                content.append({"type": "image",
                                "source": {"type": "base64", "media_type": img_media,
                                           "data": base64.b64encode(img_raw).decode()}})
            except Exception:
                continue

        if not content:
            return jsonify({"error":
                "Could not load images from URL. For Canva, export as PDF then upload directly."}), 400

    extra  = f"- Source URL: {url}"
    prompt = build_lookbook_prompt(tier_name, style_notes, cats, extra)
    content.append({"type": "text", "text": prompt})

    try:
        client   = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=2000,
            messages=[{"role": "user", "content": content}],
        )
        text  = strip_fences(response.content[0].text.strip())
        items = json.loads(text)
        if not isinstance(items, list):
            raise ValueError("Expected a list")
        return jsonify({"items": items})
    except json.JSONDecodeError as e:
        print(f"URL analyze JSON error: {e}")
        return jsonify({"error": "Could not parse AI response"}), 500
    except Exception as e:
        print(f"URL analyze error: {e}")
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    print()
    print("+=================================================+")
    print("|      E-Craft Design Tracker - AI Server        |")
    print("|  Open your browser to:  http://localhost:5000  |")
    print("|  Press Ctrl+C to stop                          |")
    print("+=================================================+")
    print()
    if ANTHROPIC_API_KEY:
        print("  API key loaded - AI suggestions active")
    else:
        print("  No API key - set ANTHROPIC_API_KEY environment variable")
    print()
    app.run(host="0.0.0.0", port=5000, debug=False)
