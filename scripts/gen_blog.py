#!/usr/bin/env python3
"""Generate individual article pages for the Rose SLI Journal.

The Journal hub (blog.html) lists every article as an excerpt card. Each card
links to a standalone, individually-indexable page under /blog/<slug>. This
script is the single source of truth for those standalone pages: it parses the
article bodies out of blog.html and renders one self-contained HTML file per
article into blog/, all sharing identical nav/footer chrome so the pages can
never drift apart (the duplicate-nav class of bug).

Run it whenever an article body changes or the shared chrome is updated:

    python scripts/gen_blog.py

To add a future monthly post: add an <article> block to scripts/articles-src.html
(the article source of truth since 2026-07-25; blog.html is a cards-only hub whose
inline bodies were removed) plus an entry to SLUGS below, then re-run. Also add an
excerpt card for the new post to blog.html by hand. The script also prints the sitemap <url> blocks for
the generated pages so they can be pasted into sitemap.xml.
"""
import os
import re

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BLOG_DIR = os.path.join(BASE_DIR, "blog")
SRC = os.path.join(BASE_DIR, "scripts", "articles-src.html")
SITE = "https://rosesli.com"

# anchor id in blog.html -> (url slug, SEO meta description)
# Order is newest-first; it drives the Newer/Older navigation between articles.
SLUGS = [
    ("who-pays", "who-pays-asl-interpreter-ada",
     "Who pays for an ASL interpreter under the ADA? The business or provider does - never the Deaf customer. What San Diego businesses need to know, including the small-business tax credit."),
    ("medical-request", "request-asl-interpreter-medical-appointment",
     "How to request an ASL interpreter for a medical appointment in San Diego: who to ask, what to say, what the ADA requires of providers, and when to insist on on-site over video."),
    ("graduation", "graduation-asl-access-san-diego",
     "Planning a San Diego graduation? How schools and colleges provide qualified ASL interpreters at commencement — and why booking early is everything."),
    ("ai", "ai-sign-language-interpreters",
     "AI sign language avatars are going viral, but Deaf people are skeptical. What the technology can and cannot do — and why human ASL interpreters still matter."),
    ("women", "deaf-women-who-shaped-asl",
     "From Alice Cogswell to Deaf President Now, the history of ASL runs through the Deaf women who taught, performed, and advocated to carry the language forward."),
    ("black-deaf-history", "black-deaf-history-asl",
     "Black ASL is a living archive of the language's history. The overlooked Black Deaf leaders who shaped modern ASL — and why it matters for interpreters."),
    ("resolution", "interpreter-access-plan-2026",
     "Make interpreter access a 2026 resolution. A practical checklist to build your ASL interpreter-request process before a Deaf client, patient, or employee arrives."),
    ("performances", "interpreted-performances-holiday-events",
     "Interpreted performances are their own craft. How signed concerts, plays, and holiday services come together in San Diego — and why prep is everything."),
    ("hospital", "deaf-patient-hospital-plan-san-diego",
     "Does your San Diego hospital have a plan when a Deaf patient walks in? What the ADA requires, where facilities fall short, and what good preparation looks like."),
    ("dei", "dei-deaf-disabled-employees",
     "DEI may be in retreat, but the ADA hasn't changed a word. What San Diego employers still owe Deaf and disabled employees — and why it just got more urgent."),
    ("deaf-awareness", "deaf-awareness-month",
     "September is Deaf Awareness Month. Its history, what Deaf culture and ASL really mean, and how to turn awareness into reliable access in San Diego."),
    ("school", "san-diego-schools-deaf-students",
     "IDEA, Section 504, and the ADA all converge in one classroom. What San Diego schools owe Deaf students — and where access quietly breaks down each fall."),
    ("ada35", "ada-at-35-san-diego-compliance",
     "The ADA turns 35. Is your San Diego business actually compliant for Deaf access, or just hoping nobody notices? What the law requires and how to get it right."),
    ("summer", "summer-event-asl-access-san-diego",
     "Planning a San Diego festival, wedding, or Pride event? How to plan ASL access for summer events — and why booking interpreters early wins."),
]

MONTHS = {
    "01": "January", "02": "February", "03": "March", "04": "April",
    "05": "May", "06": "June", "07": "July", "08": "August",
    "09": "September", "10": "October", "11": "November", "12": "December",
}


def strip_entities(s):
    """Approximate plain text from an HTML fragment for use in JSON-LD/OG."""
    s = re.sub(r"<[^>]+>", "", s)
    repl = {
        "&rsquo;": "’", "&lsquo;": "‘", "&ldquo;": "“",
        "&rdquo;": "”", "&mdash;": "—", "&ndash;": "–",
        "&amp;": "&", "&nbsp;": " ", "&hellip;": "…", "&em;": "",
        "<em>": "", "</em>": "",
    }
    for k, v in repl.items():
        s = s.replace(k, v)
    return re.sub(r"\s+", " ", s).strip()


def json_escape(s):
    return s.replace("\\", "\\\\").replace('"', '\\"')


def parse_articles(html):
    """Return {anchor: dict(cat, title, date_display, date_iso, body)}."""
    out = {}
    pattern = re.compile(
        r'<article class="blog-article" id="([^"]+)">(.*?)</article>',
        re.DOTALL,
    )
    for anchor, inner in pattern.findall(html):
        cat = re.search(r'<div class="article-cat">(.*?)</div>', inner, re.DOTALL).group(1).strip()
        title = re.search(r"<h2>(.*?)</h2>", inner, re.DOTALL).group(1).strip()
        date_display = re.search(r'<div class="article-date">(.*?)</div>', inner, re.DOTALL).group(1).strip()
        # body = everything after the article-date div, minus the back-to-top link
        after_date = inner.split('</div>', 2)[-1]
        body = re.sub(r'<a class="back-top".*?</a>\s*', "", after_date, flags=re.DOTALL).strip()
        # Article bodies use root-relative-by-accident links like href="request.html",
        # which were fine on the flat /blog page but would resolve to /blog/request.html
        # on a nested article URL. Absolutize them so in-body CTAs work everywhere.
        body = re.sub(r'href="([a-z][a-z0-9-]*)\.html"', r'href="/\1"', body)
        out[anchor] = {
            "cat": cat,
            "title": title,
            "date_display": date_display,
            "body": body,
        }
    return out


def iso_date(date_display):
    # "May 12, 2026" -> "2026-05-12"
    m = re.match(r"([A-Za-z]+)\s+(\d{1,2}),\s+(\d{4})", date_display)
    month_name, day, year = m.group(1), int(m.group(2)), m.group(3)
    num = next(k for k, v in MONTHS.items() if v == month_name)
    return f"{year}-{num}-{day:02d}"


PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<!-- Google Analytics 4 -->
<script async src="https://www.googletagmanager.com/gtag/js?id=G-SDVM4N2F3T"></script>
<script>
  window.dataLayer = window.dataLayer || [];
  function gtag(){{dataLayer.push(arguments);}}
  gtag('js', new Date());
  gtag('config', 'G-SDVM4N2F3T');
</script>
<title>{title_plain} · Rose Sign Language Interpreting</title>
<meta name="description" content="{description}">
<link rel="canonical" href="{url}">
<meta name="robots" content="index,follow,max-image-preview:large">
<meta property="og:type" content="article">
<meta property="og:site_name" content="Rose Sign Language Interpreting">
<meta property="og:title" content="{title_plain}">
<meta property="og:description" content="{description}">
<meta property="og:url" content="{url}">
<meta property="article:published_time" content="{date_iso}">
<meta property="article:section" content="{cat_plain}">
<meta name="twitter:card" content="summary_large_image">
<meta name="twitter:title" content="{title_plain}">
<meta name="twitter:description" content="{description}">
<meta property="og:image" content="{site}/og-image.png">
<meta name="twitter:image" content="{site}/og-image.png">
<link rel="icon" type="image/svg+xml" href="/favicon.svg">
<script type="application/ld+json">
{{
  "@context": "https://schema.org",
  "@type": "BlogPosting",
  "headline": "{title_json}",
  "description": "{desc_json}",
  "datePublished": "{date_iso}",
  "dateModified": "{date_iso}",
  "articleSection": "{cat_json}",
  "url": "{url}",
  "mainEntityOfPage": {{ "@type": "WebPage", "@id": "{url}" }},
  "image": "{site}/og-image.png",
  "author": {{ "@type": "Organization", "name": "Rose Sign Language Interpreting", "url": "{site}/" }},
  "publisher": {{ "@type": "Organization", "name": "Rose Sign Language Interpreting", "url": "{site}/", "logo": {{ "@type": "ImageObject", "url": "{site}/logo.svg" }} }}
}}
</script>
<script type="application/ld+json">
{{
  "@context": "https://schema.org",
  "@type": "BreadcrumbList",
  "itemListElement": [
    {{ "@type": "ListItem", "position": 1, "name": "Home", "item": "{site}/" }},
    {{ "@type": "ListItem", "position": 2, "name": "Journal", "item": "{site}/blog" }},
    {{ "@type": "ListItem", "position": 3, "name": "{title_json}", "item": "{url}" }}
  ]
}}
</script>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Instrument+Serif:ital@0;1&family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet">
<link rel="stylesheet" href="/portal.css"/>
<link rel="stylesheet" href="/site.css">
<style>
  .article-page{{max-width:760px;margin:0 auto;padding:8px 28px 8px;}}
  .article-cat{{font-size:13px;letter-spacing:.16em;text-transform:uppercase;color:var(--blue);font-weight:700;}}
  .article-page h1{{font-family:'Times New Roman',Times,serif;font-size:clamp(32px,5vw,50px);line-height:1.06;font-weight:400;letter-spacing:-.014em;margin:12px 0 12px;color:var(--ink);}}
  .article-date{{font-size:13px;letter-spacing:.14em;text-transform:uppercase;color:var(--mute);font-weight:600;margin-bottom:34px;font-variant-numeric:tabular-nums;}}
  .article-page p{{font-size:18px;line-height:1.78;color:var(--ink2);margin-bottom:22px;}}
  .article-page p.dek{{font-family:'Instrument Serif',serif;font-size:24px;line-height:1.45;color:var(--ink);font-style:italic;font-weight:400;margin-bottom:28px;}}
  .article-page h4{{font-size:14px;letter-spacing:.14em;text-transform:uppercase;color:var(--blue);font-weight:700;margin:36px 0 12px;}}
  .article-page ul{{margin:0 0 22px 22px;}}
  .article-page li{{font-size:18px;line-height:1.7;color:var(--ink2);margin-bottom:9px;}}
  .article-page a{{color:var(--blue);font-weight:600;text-decoration:none;}}
  .article-page a:hover{{text-decoration:underline;}}
  .article-nav{{max-width:760px;margin:48px auto 0;padding:28px 28px 0;border-top:1px solid var(--rule);display:flex;justify-content:space-between;gap:20px;}}
  .article-nav a{{display:flex;flex-direction:column;gap:5px;text-decoration:none;max-width:46%;}}
  .article-nav .dir{{font-size:11px;letter-spacing:.14em;text-transform:uppercase;font-weight:700;color:var(--blue);}}
  .article-nav .ttl{{font-size:14px;line-height:1.3;color:var(--ink2);font-weight:600;}}
  .article-nav a:hover .ttl{{color:var(--ink);}}
  .article-nav .next{{text-align:right;margin-left:auto;align-items:flex-end;}}
  .all-articles{{max-width:760px;margin:24px auto 0;padding:0 28px;}}
  .all-articles a{{font-size:12px;letter-spacing:.12em;text-transform:uppercase;font-weight:700;color:var(--mute);text-decoration:none;}}
  .all-articles a:hover{{color:var(--ink);}}
</style>
</head>
<body>

<div class="topbar">
  <div class="left"><span><span class="dot"></span>Accepting new bookings this week</span></div>
  <div class="right">
    <span>In person for San Diego County and VRI nationwide</span>
    <span><a href="tel:+18582636719" style="color:inherit;text-decoration:none;">858-263-6719</a></span>
    <span>info@rosesli.com</span>
  </div>
  <span class="topbar-request-pill"><a href="/request">Request Interpreter</a></span>
</div>

<nav class="main">
  <a href="/" class="logo">
    <div class="logo-mark">r</div>
    <div class="logo-text">Rose <em>Sign Language Interpreting</em></div>
  </a>
  <ul class="nav-links">
    <li><a href="/about">About</a></li>
    <li><a href="/testimonials">Testimonials</a></li>
    <li><a href="/vri">VRI</a></li>
    <li><a href="/blog" class="active">Journal</a></li>
    <li><a href="/request" class="nav-cta nav-cta-lg">Request an interpreter <svg class="arrow" viewBox="0 0 16 16" fill="currentColor"><path d="M6 3l5 5-5 5V3z"/></svg></a></li>
  </ul>
  <div id="nav-auth" style="position:relative;"></div>
  <button class="nav-hamburger" id="nav-hamburger" aria-label="Open menu" aria-expanded="false"><span></span><span></span><span></span></button>
</nav>
<div class="nav-mobile-menu" id="nav-mobile-menu" role="navigation" aria-label="Mobile menu">
  <a href="/">Home</a>
  <a href="/about">About</a>
  <a href="/testimonials">Testimonials</a>
  <a href="/vri">VRI</a>
  <a href="/blog" class="active">Journal</a>
  <a href="/login">Login</a>
</div>
<script>
(function () {{
  var el = document.getElementById('nav-auth');
  function showUser(name) {{
    el.textContent = '';
    var wrap = document.createElement('div');
    wrap.className = 'nav-user-info';
    var aPortal = document.createElement('a');
    aPortal.href = '/portal'; aPortal.textContent = 'Portal';
    var aLogout = document.createElement('a');
    aLogout.href = '/logout'; aLogout.textContent = 'Logout';
    wrap.appendChild(aPortal); wrap.appendChild(aLogout);
    el.appendChild(wrap);
  }}
  function showForm(errMsg) {{
    el.textContent = '';
    var form = document.createElement('form');
    form.className = 'nav-login-form';
    var emailIn = document.createElement('input');
    emailIn.type = 'email'; emailIn.name = 'email';
    emailIn.placeholder = 'email'; emailIn.required = true;
    var passIn = document.createElement('input');
    passIn.type = 'password'; passIn.name = 'password'; passIn.placeholder = 'password'; passIn.required = true;
    var btn = document.createElement('button');
    btn.type = 'submit'; btn.textContent = '→';
    form.appendChild(emailIn); form.appendChild(passIn); form.appendChild(btn);
    if (errMsg) {{
      var err = document.createElement('span');
      err.className = 'nav-login-error'; err.textContent = errMsg;
      form.appendChild(err);
    }}
    el.appendChild(form);
    var forgot = document.createElement('a');
    forgot.href = '/forgot-password'; forgot.textContent = 'Forgot password?';
    forgot.style.cssText = 'font-size:.72rem;color:var(--muted);text-decoration:none;display:block;text-align:right;margin-top:3px;';
    el.appendChild(forgot);
    form.addEventListener('submit', function (e) {{
      e.preventDefault();
      fetch('/api/csrf-token')
        .then(function (r) {{ return r.json(); }})
        .then(function (csrf) {{
          return fetch('/login', {{
            method: 'POST',
            body: new FormData(form),
            headers: {{ 'X-CSRF-Token': csrf.csrf_token }}
          }});
        }})
        .then(function (r) {{
          return r.text().then(function (body) {{
            var d = null;
            try {{ d = JSON.parse(body); }} catch (e) {{}}
            if (d && d.ok) {{ window.location.href = d.redirect || '/portal'; return; }}
            showForm(d && d.error ? d.error : 'Server error (' + r.status + '). Try again.');
          }});
        }})
        .catch(function () {{ showForm('Network error. Try again.'); }});
    }});
  }}
  fetch('/api/me')
    .then(function (r) {{ return r.ok ? r.json() : null; }})
    .then(function (d) {{ d && d.ok ? showUser(d.user.name) : showForm(); }})
    .catch(function () {{ showForm(); }});
}})();
(function(){{var b=document.getElementById('nav-hamburger'),m=document.getElementById('nav-mobile-menu');if(!b||!m)return;b.addEventListener('click',function(){{var o=m.classList.toggle('open');b.classList.toggle('open',o);b.setAttribute('aria-expanded',o?'true':'false');}});document.addEventListener('click',function(e){{if(!b.contains(e.target)&&!m.contains(e.target)){{m.classList.remove('open');b.classList.remove('open');b.setAttribute('aria-expanded','false');}}}});}})();
</script>

<div class="crumbs"><a href="/">Home</a><span class="sep">›</span><a href="/blog">Journal</a><span class="sep">›</span>{title_plain}</div>

<article class="article-page">
  <div class="article-cat">{cat}</div>
  <h1>{title}</h1>
  <div class="article-date">{date_display}</div>
  {body}
</article>

<nav class="article-nav" aria-label="More articles">
{prev_html}{next_html}</nav>
<div class="all-articles"><a href="/blog">← All articles</a></div>

<!-- CTA -->
<section class="page-cta">
  <div class="page-cta-inner">
    <h3>Have a setting that needs <span class="serif">a careful match?</span></h3>
    <a href="/request" class="btn btn-dark">Request an interpreter <svg class="arrow" viewBox="0 0 16 16" fill="currentColor"><path d="M6 3l5 5-5 5V3z"/></svg></a>
  </div>
</section>

<footer>
  <div class="foot-grid">
    <div class="foot-brand">
      <div class="logo-text">Rose <em>Sign Language Interpreting</em></div>
      <p>Certified ASL interpreting across San Diego County and nationwide via video. Founded by Amanda Rose.</p>
      <div class="foot-contact"><a href="tel:+18582636719">858-263-6719</a> · info@rosesli.com · San Diego, CA</div>
    </div>
    <div class="foot-creds">
      <img src="/rid-logo.png" alt="RID" class="rid-mark">
      <img src="/nad-logo.png" alt="NAD" class="nad-mark">
      <span class="foot-creds-lbl">RID and NAD certified interpreters</span>
    </div>
  </div>
  <div class="foot-bottom"><span>© 2026 Rose Sign Language Interpreting</span></div>
</footer>

</body>
</html>
"""


def main():
    with open(SRC, encoding="utf-8") as f:
        html = f.read()
    parsed = parse_articles(html)

    os.makedirs(BLOG_DIR, exist_ok=True)
    sitemap_lines = []

    for i, (anchor, slug, desc) in enumerate(SLUGS):
        if anchor not in parsed:
            raise SystemExit(f"Article id '{anchor}' not found in articles-src.html")
        a = parsed[anchor]
        url = f"{SITE}/blog/{slug}"
        date_iso = iso_date(a["date_display"])
        title_plain = strip_entities(a["title"])
        cat_plain = strip_entities(a["cat"])

        # Newer (prev) and Older (next) navigation
        prev_html = ""
        next_html = ""
        if i > 0:
            p_slug = SLUGS[i - 1][1]
            p_title = strip_entities(parsed[SLUGS[i - 1][0]]["title"])
            prev_html = (
                f'  <a class="prev" href="/blog/{p_slug}">'
                f'<span class="dir">← Newer</span>'
                f'<span class="ttl">{p_title}</span></a>\n'
            )
        if i < len(SLUGS) - 1:
            n_slug = SLUGS[i + 1][1]
            n_title = strip_entities(parsed[SLUGS[i + 1][0]]["title"])
            next_html = (
                f'  <a class="next" href="/blog/{n_slug}">'
                f'<span class="dir">Older →</span>'
                f'<span class="ttl">{n_title}</span></a>\n'
            )

        page = PAGE.format(
            title=a["title"],
            title_plain=title_plain,
            title_json=json_escape(title_plain),
            cat=a["cat"],
            cat_plain=cat_plain,
            cat_json=json_escape(cat_plain),
            description=desc,
            desc_json=json_escape(desc),
            date_display=a["date_display"],
            date_iso=date_iso,
            body=a["body"],
            url=url,
            site=SITE,
            prev_html=prev_html,
            next_html=next_html,
        )
        out_path = os.path.join(BLOG_DIR, f"{slug}.html")
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(page)
        print(f"wrote blog/{slug}.html  ({date_iso})")

        sitemap_lines.append(
            f"  <url>\n    <loc>{url}</loc>\n    <lastmod>{date_iso}</lastmod>\n"
            f"    <changefreq>yearly</changefreq>\n    <priority>0.7</priority>\n  </url>"
        )

    print("\n--- sitemap <url> blocks (paste into sitemap.xml) ---")
    print("\n".join(sitemap_lines))


if __name__ == "__main__":
    main()
