"""The classifier's decision-model provider (the System One format).

A decision model is not a chat model: POST {"state", "model", "questions"}
to the base URL as given (the full endpoint), the key as a bearer token,
and it answers each question with a probability (a Noul's, or a Choice's
{option: probability}), every question of a request in isolation against
the same state. Its questions are atomic and code combines them.

Here: what a call lacks (key, endpoint, model), the request as plain JSON,
the call itself with its error mapping (held, tried again, refused), the
answers read back, and the site and contacts tasks' questions and the
rules that combine their answers. harvey/classify.py registers the tasks,
paces, caps, retries and records every attempt; the shared pieces (the
key, the URL check, the HTTP client, the held and transient errors) are
read from it inside a call, so this module imports nothing from it when
it loads.
"""

from __future__ import annotations


def missing(s) -> str:
    """What a decision call lacks ("" when nothing): its key, endpoint or
    model. None is filled in for the user."""
    from harvey import classify

    if not s.base_url:
        return "no endpoint: enter the decision model's full URL"
    if not s.model:
        return "no model: enter the decision model's name"
    if not classify._key(s):
        return f"no key in {s.key_name}"
    return ""


def plain(questions: dict) -> dict:
    """The questions as the System One format sends them: {"type",
    "instructions"} and "criteria" when a question has them."""
    out = {}
    for qid, q in questions.items():
        q = dict(q)
        item = {"type": str(q["type"]), "instructions": str(q["instructions"])}
        if q.get("criteria"):
            item["criteria"] = {str(k): str(v) for k, v in dict(q["criteria"]).items()}
        out[str(qid)] = item
    return out


async def send(s, d_state, questions: dict) -> tuple[dict, dict]:
    """One System One request: POST {"state", "model", "questions"} to the
    base URL as given (the full endpoint), with the key as a bearer token.
    ({question id: a Noul's probability, or a Choice's {option:
    probability}}, usage). Refused key, payment, unknown model or endpoint,
    429 or a spent quota: _Held (a 429 for its retry-after-ms or
    retry-after); timeouts, lost connections and 5xx: _Transient; _call
    retries and records each attempt."""
    import httpx

    from harvey import classify

    why = classify.base_url_problem(s.base_url) or missing(s)
    if why:
        raise classify._Held(why)
    body = {"state": d_state, "model": s.model, "questions": plain(questions)}
    try:
        async with classify._http(s.timeout) as client:
            r = await client.post(s.base_url, json=body,
                                  headers={"Authorization": f"Bearer {classify._key(s)}"})
    except httpx.TimeoutException as e:
        raise classify._Transient(f"{type(e).__name__}") from e
    except httpx.TransportError as e:
        raise classify._Transient(f"{type(e).__name__}") from e
    status = r.status_code
    if status in (401, 402, 403, 404, 429) or (status >= 400 and "quota" in r.text.lower()):
        raise classify._Held(f"HTTP {status}",
                             classify._wait_header(r.headers) if status == 429 else None)
    if status >= 500:
        raise classify._Transient(f"HTTP {status}")
    if status >= 400:
        raise ValueError(f"HTTP {status}: {r.text[:200]}")
    try:
        data = r.json()
    except ValueError as e:
        raise ValueError("a reply that isn't JSON") from e
    data = data if isinstance(data, dict) else {}
    reply = {}
    for qid, a in (data.get("answers") or {}).items():
        if not isinstance(a, dict):
            continue
        kind = a.get("type", "noul" if "noul" in a else "")
        if kind == "noul":
            reply[qid] = a.get("noul")
        elif kind == "choice":
            probs = a.get("probabilities")
            reply[qid] = dict(probs) if isinstance(probs, dict) else None
    usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}

    def count(name):
        value = usage.get(name)
        return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0

    return reply, {"in": count("input_tokens"), "out": count("output_tokens"),
                   "model": str(data.get("model") or s.model)}


def asked_answers(reply: dict, ids: list[str]) -> dict:
    """A System One reply's answers to the questions asked (``ids``): a
    Noul's probability, a Choice's {option: probability}, each within 0..1;
    anything else it names is ignored. NoAnswer when none was answered."""
    from harvey.classify import NoAnswer

    def prob(v):
        return isinstance(v, (int, float)) and not isinstance(v, bool) and 0.0 <= v <= 1.0

    out = {}
    for i in ids:
        v = (reply or {}).get(i)
        if prob(v):
            out[i] = float(v)
        elif isinstance(v, dict) and v and all(prob(x) for x in v.values()):
            out[i] = {str(k): float(x) for k, x in v.items()}
    if not out:
        raise NoAnswer(f"no answer to {', '.join(ids[:20])}")
    return out


# ── The site and contacts tasks, asked of a decision model ──
#
# The wording and the bars below were chosen on half of a labelled set of
# real firms and scored once on the other half: change one and measure it
# again.

COMPANY = "the company (COMPANY)"
PAGE = "The website between <page> and </page>"
FORM = "Ignore the legal form (SRL, S.R.L., SA, PFA, II, IF), letter case and diacritics."

# A site is the firm's when it names the firm and presents its own
# business (each at least its bar), and isn't a directory or an
# institution (each below its veto).
SITE_NEEDS = (("names", 0.5), ("own", 0.4))
SITE_VETOES = (("directory", 0.7), ("institution", 0.7))

# Whose a contact read on the firm's site is: the firm's own first (the
# order matters to the model).
OWNERS = {
    "company_own": "The company's own phone, email, contact form or social profile",
    "agency_or_theme": "The web agency's, developer's or website theme's",
    "other_office": "Another office, branch or company of a group, in another country",
    "platform": "A platform, marketplace, booking or hosting service's",
    "partner_or_other": "A partner's, supplier's, client's or another business's",
    "placeholder_or_hotline": "A placeholder, example value, or a public hotline or institution",
}


def site(p: dict, budget: int) -> tuple[str, dict]:
    from harvey.classify import _render_site

    text, _ = _render_site(p, budget)
    c, w = COMPANY, PAGE
    return text, {
        "names": {"type": "noul",
                  "instructions": f"{w} names {c}: the company's distinctive name appears in "
                                  f"the website's domain, title or text. {FORM}",
                  "criteria": {"true": "the distinctive words of the company's name appear "
                                       "together as the name of the business the site "
                                       "presents.",
                               "false": "only one common word is shared, the name is "
                                        "different, or it appears only in a list of many "
                                        "companies."}},
        "own": {"type": "noul",
                "instructions": f"{w} presents the business of {c} itself: its own products "
                                f"or services, written by that business, under its name."},
        "directory": {"type": "noul",
                      "instructions": f"{w} is a company-information site, business "
                                      f"directory, listing, marketplace, classifieds, job "
                                      f"board or comparison site that lists many businesses."},
        "institution": {"type": "noul",
                        "instructions": f"{w} belongs to a city hall, public institution, "
                                        f"government body, school, association, or a news or "
                                        f"media outlet."},
    }


def check_site(said: dict, p: dict, yes: float) -> dict:
    # (Every question is needed; ``p`` is how surely the site presents the
    # firm's own business, kept with the decision.)
    from harvey.classify import NoAnswer

    probs = {q: said[q] for q, _ in SITE_NEEDS + SITE_VETOES}
    if any(not isinstance(v, float) for v in probs.values()):
        raise NoAnswer("a site question answered with options")
    ok = all(probs[q] >= at for q, at in SITE_NEEDS) and \
        all(probs[q] < below for q, below in SITE_VETOES)
    return {"yes": ok, "p": round(probs["own"], 2)}


def contacts(p: dict, budget: int) -> tuple[str, dict]:
    from harvey.classify import _render_contacts

    text, statements = _render_contacts(p, budget)
    # (ch_<j>: card.contacts[j], shown to the model as c<j+1>.)
    return text, {f"ch_{j}": {"type": "choice", "instructions": f"Whose is contact c{j + 1}?",
                              "criteria": dict(OWNERS)}
                  for j in range(len(statements))}


def check_contacts(said: dict, p: dict, yes: float) -> dict:
    # (A contact is the firm's when the model's probability that it is
    # reaches its bar; one not shown or not answered is in neither list.)
    from harvey.classify import NoAnswer

    n = min(len(p["cards"][0].contacts), p.get("shown_contacts", 12))
    seen = {j: said[f"ch_{j}"]["company_own"] for j in range(n)
            if isinstance(said.get(f"ch_{j}"), dict) and "company_own" in said[f"ch_{j}"]}
    if not seen and n:
        raise NoAnswer("no contact answered")
    return {"contacts": [j for j, v in seen.items() if v >= yes],
            "no": [j for j, v in seen.items() if v < yes]}
