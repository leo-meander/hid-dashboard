"""Ad Analyzer Service — Claude Vision + funnel analysis + recommendations."""
import json
import logging
from datetime import datetime, timezone

import anthropic

from app.config import settings

logger = logging.getLogger(__name__)

PRESET_ANGLES = [
    "Location", "Aesthetic", "Experience", "Price",
    "Community", "Eco", "Digital Nomad",
]

PRESET_TA = [
    "Solo", "Couple", "Friend Group", "Family",
    "Business", "Digital Nomad", "High Intent", "Generic",
]

MODEL = "claude-sonnet-4-6"


def build_funnel_analysis(combo) -> dict:
    """Build funnel breakdown with conversion rates and bottleneck detection."""
    impressions = combo.impressions or 0
    clicks = combo.clicks or 0
    lp_views = combo.lp_views or 0
    atc = combo.add_to_cart or 0
    checkout = combo.initiate_checkout or 0
    purchases = combo.purchases or 0

    funnel = {
        "impressions": impressions,
        "clicks": clicks,
        "ctr": round(clicks / impressions * 100, 2) if impressions > 0 else 0,
        "lp_views": lp_views,
        "lp_view_rate": round(lp_views / clicks * 100, 1) if clicks > 0 else 0,
        "add_to_cart": atc,
        "atc_rate": round(atc / lp_views * 100, 1) if lp_views > 0 else 0,
        "checkout": checkout,
        "checkout_rate": round(checkout / atc * 100, 1) if atc > 0 else 0,
        "purchases": purchases,
        "purchase_rate": round(purchases / checkout * 100, 1) if checkout > 0 else 0,
        "bottleneck": None,
        "bottleneck_message": None,
    }

    # Detect bottleneck — find the biggest drop-off
    if impressions < 5000:
        funnel["bottleneck"] = "insufficient_data"
        funnel["bottleneck_message"] = f"Only {impressions} impressions — need more data"
    elif funnel["ctr"] < 1.0:
        funnel["bottleneck"] = "ctr"
        funnel["bottleneck_message"] = f"CTR {funnel['ctr']}% is low — ad creative not compelling enough"
    elif clicks > 0 and funnel["lp_view_rate"] < 50:
        funnel["bottleneck"] = "lp_view"
        funnel["bottleneck_message"] = f"Only {funnel['lp_view_rate']}% of clicks reach LP — slow load or redirect issue"
    elif lp_views > 0 and funnel["atc_rate"] < 5:
        funnel["bottleneck"] = "add_to_cart"
        funnel["bottleneck_message"] = f"Only {funnel['atc_rate']}% add to cart — LP content or pricing issue"
    elif atc > 0 and funnel["checkout_rate"] < 50:
        funnel["bottleneck"] = "checkout"
        funnel["bottleneck_message"] = f"Only {funnel['checkout_rate']}% proceed to checkout — UX or trust issue"
    elif checkout > 0 and funnel["purchase_rate"] < 60:
        funnel["bottleneck"] = "purchase"
        funnel["bottleneck_message"] = f"Only {funnel['purchase_rate']}% complete purchase — payment or trust issue"

    return funnel


def build_recommendation(combo, funnel: dict, benchmark: float | None) -> tuple[str, str]:
    """Generate recommendation type and message based on funnel + ROAS data.

    Returns (recommendation_type, message).
    """
    roas = float(combo.roas) if combo.roas else 0
    spend = float(combo.spend_vnd) if combo.spend_vnd else 0
    impressions = combo.impressions or 0

    if impressions < 5000:
        return "insufficient_data", "Not enough data to evaluate. Need at least 5K impressions."

    if benchmark and benchmark > 0:
        if roas >= 1.5 * benchmark and spend > 20_000:
            return "scale_up", f"ROAS {roas:.2f}x is {roas/benchmark:.1f}x above benchmark ({benchmark}x). Scale up budget."
        if roas <= 0.6 * benchmark and spend > 30_000:
            return "pause", f"ROAS {roas:.2f}x is well below benchmark ({benchmark}x). Consider pausing."

    # Funnel-based recommendations
    bottleneck = funnel.get("bottleneck")
    if bottleneck == "ctr":
        return "optimize", "Low CTR — test new creative/headline to improve click-through."
    if bottleneck == "lp_view":
        return "optimize", "High click drop-off — check landing page load speed and redirects."
    if bottleneck == "add_to_cart":
        return "optimize", "LP visitors not adding to cart — review pricing display and LP content."
    if bottleneck == "checkout":
        return "optimize", "Cart abandonment — simplify checkout flow or add trust signals."
    if bottleneck == "purchase":
        return "optimize", "Checkout drop-off — check payment options and security badges."

    if roas > 0 and (not benchmark or roas >= benchmark):
        return "scale_up", f"Performing well (ROAS {roas:.2f}x). Consider increasing budget."

    return "test_new", "Performance is moderate. Test variations of copy or visual."


def analyze_combo_with_ai(
    combo,
    copy_text: str,
    headline: str,
    material_type: str,
    image_url: str | None,
    funnel: dict,
    benchmark: float | None = None,
) -> dict:
    """Call Claude API to evaluate ad performance and provide optimization recommendations.

    Returns dict with: detected_angles, detected_ta, keypoints, visual_summary,
    performance_evaluation, optimization_actions, confidence
    """
    if not settings.ANTHROPIC_API_KEY:
        logger.warning("ANTHROPIC_API_KEY not set — skipping AI analysis")
        return {}

    client = anthropic.Anthropic(api_key=settings.ANTHROPIC_API_KEY)

    roas = float(combo.roas) if combo.roas else 0
    spend = float(combo.spend_vnd) if combo.spend_vnd else 0
    revenue = float(combo.revenue_vnd) if combo.revenue_vnd else 0
    cpc = round(spend / combo.clicks, 0) if combo.clicks and combo.clicks > 0 else 0
    cpa = round(spend / combo.purchases, 0) if combo.purchases and combo.purchases > 0 else 0

    prompt = f"""You are a senior hotel performance marketer. Evaluate this ad campaign based on real data from the last 14 days and provide actionable optimization recommendations.

═══ AD CREATIVE ═══
Headline: {headline or '(none)'}
Primary Text: {copy_text or '(none)'}
Material Type: {material_type}
Meta Ad Name: {combo.meta_ad_name or '(not linked)'}

═══ PERFORMANCE METRICS (Last 14 days) ═══
Spend: {spend:,.0f} VND
Revenue: {revenue:,.0f} VND
ROAS: {roas:.2f}x {f'(Branch benchmark: {benchmark}x)' if benchmark else '(no benchmark)'}
Impressions: {funnel['impressions']:,}
Clicks: {funnel['clicks']:,}
CTR: {funnel['ctr']}%
CPC: {cpc:,.0f} VND
Purchases: {combo.purchases or 0}
CPA: {cpa:,.0f} VND per purchase

═══ CONVERSION FUNNEL ═══
Impressions: {funnel['impressions']:,}
  → Clicks: {funnel['clicks']:,} (CTR: {funnel['ctr']}%)
    → Landing Page Views: {funnel['lp_views']:,} ({funnel['lp_view_rate']}% of clicks)
      → Add to Cart: {funnel['add_to_cart']:,} ({funnel['atc_rate']}% of LP views)
        → Checkout: {funnel['checkout']:,} ({funnel['checkout_rate']}% of ATC)
          → Purchase: {funnel['purchases']:,} ({funnel['purchase_rate']}% of checkout)

═══ INDUSTRY BENCHMARKS FOR HOTEL ADS ═══
Good CTR: >1.5%  |  Great CTR: >3%
Good LP View Rate: >70%  |  Concern: <50%
Good ATC Rate: >8%  |  Concern: <3%
Good Checkout Rate: >60%  |  Concern: <40%
Good Purchase Rate: >70%  |  Concern: <50%

Respond in JSON format:
{{
  "detected_angles": ["angle1"],
  "detected_ta": ["ta1"],
  "keypoints": ["selling point 1", "selling point 2", "selling point 3"],
  "visual_summary": "What this ad communicates in 1 sentence",

  "performance_verdict": "STRONG|MODERATE|WEAK|INSUFFICIENT_DATA",
  "performance_summary": "2-3 sentence overall performance assessment comparing to benchmarks",

  "funnel_diagnosis": "Which funnel stage is the biggest bottleneck and why (1-2 sentences)",

  "optimization_actions": [
    {{
      "priority": "HIGH|MEDIUM|LOW",
      "area": "Creative|Targeting|Landing Page|Budget|Bidding|Funnel",
      "action": "Specific actionable recommendation (1-2 sentences)",
      "expected_impact": "What improvement to expect (e.g. +0.5% CTR, +20% conversions)"
    }}
  ],

  "budget_recommendation": "SCALE_UP|MAINTAIN|REDUCE|PAUSE",
  "budget_reasoning": "Why this budget action (1 sentence)",

  "testing_suggestions": [
    "Specific A/B test idea 1",
    "Specific A/B test idea 2"
  ],

  "confidence": 0.85
}}

Rules:
- Give 3-5 optimization_actions sorted by priority (HIGH first)
- Be specific to THIS ad — reference the actual headline/copy content
- If data is insufficient (<5K impressions), say so and recommend waiting
- Compare metrics against the benchmarks provided
- For hotel ads: focus on booking conversions, not just clicks
Only return valid JSON, no markdown."""

    # Build messages — use image if available
    messages_content = []
    if image_url:
        try:
            messages_content.append({
                "type": "image",
                "source": {"type": "url", "url": image_url},
            })
        except Exception:
            pass  # Skip image if URL is invalid

    messages_content.append({"type": "text", "text": prompt})

    try:
        response = client.messages.create(
            model=MODEL,
            max_tokens=2048,
            messages=[{"role": "user", "content": messages_content}],
        )
        text = response.content[0].text.strip()
        # Clean markdown code blocks if present
        if text.startswith("```"):
            text = text.split("\n", 1)[1].rsplit("```", 1)[0].strip()
        result = json.loads(text)
        result["model_used"] = MODEL
        return result
    except Exception as e:
        logger.error("AI analysis failed: %s", e)
        return {}


def run_analysis(combo, db_session, benchmark: float | None = None) -> dict:
    """Full analysis pipeline for one combo. Returns analysis result dict."""
    copy = combo.copy
    material = combo.material

    copy_text = copy.primary_text if copy else ""
    headline = copy.headline if copy else ""
    material_type = material.material_type if material else "unknown"
    image_url = material.file_link if material else None

    # Step 1: Build funnel analysis
    funnel = build_funnel_analysis(combo)

    # Step 2: AI analysis (angles, TA, keypoints, performance eval, optimization)
    ai_result = analyze_combo_with_ai(
        combo, copy_text, headline, material_type, image_url, funnel, benchmark,
    )

    # Step 3: Generate recommendation — prefer AI's budget_recommendation if available
    rec_type, rec_message = build_recommendation(combo, funnel, benchmark)
    # Override with AI recommendation if present
    ai_budget = ai_result.get("budget_recommendation", "").lower().replace("_", "_")
    if ai_budget in ("scale_up", "maintain", "reduce", "pause"):
        rec_type = ai_budget
    ai_summary = ai_result.get("performance_summary", "")
    if ai_summary:
        rec_message = ai_summary

    # Merge AI optimization data into funnel_analysis for frontend display
    funnel["performance_verdict"] = ai_result.get("performance_verdict", "")
    funnel["performance_summary"] = ai_result.get("performance_summary", "")
    funnel["funnel_diagnosis"] = ai_result.get("funnel_diagnosis", "")
    funnel["optimization_actions"] = ai_result.get("optimization_actions", [])
    funnel["budget_recommendation"] = ai_result.get("budget_recommendation", "")
    funnel["budget_reasoning"] = ai_result.get("budget_reasoning", "")
    funnel["testing_suggestions"] = ai_result.get("testing_suggestions", [])

    return {
        "combo_id": combo.id,
        "detected_angles": ai_result.get("detected_angles", []),
        "detected_ta": ai_result.get("detected_ta", []),
        "keypoints": ai_result.get("keypoints", []),
        "visual_summary": ai_result.get("visual_summary", ""),
        "funnel_analysis": funnel,
        "ai_recommendation": rec_message,
        "recommendation_type": rec_type,
        "confidence_score": ai_result.get("confidence", 0.5),
        "model_used": ai_result.get("model_used", MODEL),
        "analyzed_at": datetime.now(timezone.utc),
    }
