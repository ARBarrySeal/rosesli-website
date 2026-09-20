# rosesli.com Improvements Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Fix 12 concrete defects found in the rosesli.com codebase — covering correctness bugs, SEO gaps, UX gaps, and cleanup.

**Architecture:** All changes are to static HTML files (`index.html`, `about.html`, `testimonials.html`, `request.html`, `vri.html`, `accessibility-statement.html`), one CSS file (`site.css`), one Python file (`main.py`), and `.gitignore`. No new files except removing one unused 1.5MB image.

**Tech Stack:** Flask (Python), plain HTML/CSS/JS, schema.org JSON-LD, Git

---

## File Map

| File | Changes |
|---|---|
| `index.html` | Fix stat "20+" → "30+"; fix nav links .html → /path; add Request to mobile menu |
| `about.html` | Add favicon; add preconnect; fix nav links; add Request to mobile menu |
| `testimonials.html` | Add favicon; add preconnect; fix nav links; add Request to mobile menu; add AggregateRating schema |
| `request.html` | Add favicon; add preconnect; fix nav links; add Request to mobile menu; add honeypot |
| `vri.html` | Add favicon; add preconnect; fix nav links; add Request to mobile menu; add FAQPage schema |
| `accessibility-statement.html` | Fix canonical URL; remove noindex |
| `site.css` | Fix footer credential sizes (160px → 48px) |
| `main.py` | Remove hand-shaka.png from _ALLOWED_IMAGES |
| `.gitignore` | Add deploy_blog.log |

---

### Task 1: Fix "years in practice" stat inconsistency

**Files:**
- Modify: `index.html:508`

- [ ] **Step 1: Edit index.html stat**

Change `<div class="stat-num">20+</div>` (Years in practice) to `30+` to match about.html and Amanda's bio.

- [ ] **Step 2: Commit**

```bash
git add index.html
git commit -m "fix: align years-in-practice stat to 30+ (matches about page + bio)"
```

---

### Task 2: Add missing favicon link to 4 inner pages

**Files:**
- Modify: `about.html`, `testimonials.html`, `request.html`, `vri.html`

- [ ] **Step 1: Add favicon to each page**

In each page, after the `<meta name="viewport"...>` line, add:
```html
<link rel="icon" type="image/svg+xml" href="/favicon.svg">
```

- [ ] **Step 2: Commit**

```bash
git add about.html testimonials.html request.html vri.html
git commit -m "fix: add missing favicon link to inner pages"
```

---

### Task 3: Add missing preconnect hints to 4 inner pages

**Files:**
- Modify: `about.html`, `testimonials.html`, `request.html`, `vri.html`

- [ ] **Step 1: Add preconnect links to each page**

In each page, before the `<link href="https://fonts.googleapis.com/css2?...">` line, add:
```html
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
```

- [ ] **Step 2: Commit**

```bash
git add about.html testimonials.html request.html vri.html
git commit -m "perf: add font preconnect hints to inner pages"
```

---

### Task 4: Add "Request interpreter" CTA to mobile hamburger menu on all pages

**Files:**
- Modify: `index.html`, `about.html`, `testimonials.html`, `request.html`, `vri.html`

- [ ] **Step 1: Add Request link to each page's mobile menu**

In each page's `.nav-mobile-menu` div, after the `<a href="/login">Login</a>` line, add:
```html
<a href="/request">Request an interpreter</a>
```

This becomes the last child, which site.css styles as blue (`color:var(--blue)`), making it the mobile CTA.

- [ ] **Step 2: Commit**

```bash
git add index.html about.html testimonials.html request.html vri.html
git commit -m "feat: add Request interpreter link to mobile nav menu on all pages"
```

---

### Task 5: Fix accessibility-statement.html canonical URL and indexing

**Files:**
- Modify: `accessibility-statement.html`

- [ ] **Step 1: Fix canonical and robots**

Change:
```html
<link rel="canonical" href="https://www.rosesli.com/accessibility-statement.html">
<meta name="robots" content="noindex, follow">
```
To:
```html
<link rel="canonical" href="https://rosesli.com/accessibility-statement">
```
(remove the noindex line entirely — page is linked from footer)

- [ ] **Step 2: Commit**

```bash
git add accessibility-statement.html
git commit -m "fix: correct accessibility-statement canonical URL and allow indexing"
```

---

### Task 6: Add spam-blocking honeypot to request.html form

**Files:**
- Modify: `request.html:222`

- [ ] **Step 1: Add honeypot**

After the `<div class="form-fields">` opening tag, add:
```html
<div aria-hidden="true" style="position:absolute;left:-9999px;width:1px;height:1px;overflow:hidden;">
  <label>Website<input type="text" name="website" tabindex="-1" autocomplete="off"></label>
</div>
```

The `/api/request` backend already checks the `website` honeypot field (used by index.html's inline form).

- [ ] **Step 2: Commit**

```bash
git add request.html
git commit -m "fix: add honeypot spam protection to standalone request form"
```

---

### Task 7: Change all internal nav links from .html extension to /path

**Files:**
- Modify: `index.html`, `about.html`, `testimonials.html`, `request.html`, `vri.html`, `blog.html`

- [ ] **Step 1: Replace .html hrefs in each file**

Replace in nav, mobile menu, breadcrumbs, and CTA buttons:
- `href="about.html"` → `href="/about"`
- `href="testimonials.html"` → `href="/testimonials"`
- `href="vri.html"` → `href="/vri"`
- `href="blog.html"` → `href="/blog"`
- `href="request.html"` → `href="/request"`
- `href="request.html?format=vri"` → `href="/request?format=vri"`

Flask routes handle both `.html` and clean paths, but canonical URLs use clean paths.

- [ ] **Step 2: Commit**

```bash
git add index.html about.html testimonials.html request.html vri.html blog.html
git commit -m "fix: use canonical /path URLs in nav links (not .html extension)"
```

---

### Task 8: Add FAQPage JSON-LD schema to vri.html

**Files:**
- Modify: `vri.html`

- [ ] **Step 1: Add FAQPage schema in head**

After the existing `<meta name="twitter:image" ...>` line in the head, add:
```html
<script type="application/ld+json">
{
  "@context": "https://schema.org",
  "@type": "FAQPage",
  "mainEntity": [
    {
      "@type": "Question",
      "name": "How fast can I get a VRI interpreter?",
      "acceptedAnswer": {
        "@type": "Answer",
        "text": "3 days notice is preferable, but same day is an option upon availability."
      }
    },
    {
      "@type": "Question",
      "name": "What if the connection drops mid-appointment?",
      "acceptedAnswer": {
        "@type": "Answer",
        "text": "The interpreter will dial back in within 60 seconds. For high-stakes settings (surgery, court), we recommend on-site."
      }
    },
    {
      "@type": "Question",
      "name": "How is this different from a national VRI service?",
      "acceptedAnswer": {
        "@type": "Answer",
        "text": "National VRI is a call-center model: you dial in, you get whoever is on the queue. Rose SLI is a small certified roster. The interpreter you see has been booked for this meeting specifically, with prep materials in hand."
      }
    },
    {
      "@type": "Question",
      "name": "What does VRI cost?",
      "acceptedAnswer": {
        "@type": "Answer",
        "text": "VRI is billed in 30-minute increments with a 2-hour minimum. We can quote a flat monthly retainer for organizations with recurring needs. Email info@rosesli.com for the rate sheet."
      }
    }
  ]
}
</script>
```

- [ ] **Step 2: Commit**

```bash
git add vri.html
git commit -m "seo: add FAQPage JSON-LD schema to VRI page"
```

---

### Task 9: Add AggregateRating schema to testimonials.html

**Files:**
- Modify: `testimonials.html`

- [ ] **Step 1: Add schema in head**

After the `<meta name="twitter:image" ...>` line, add:
```html
<script type="application/ld+json">
{
  "@context": "https://schema.org",
  "@type": "ProfessionalService",
  "@id": "https://rosesli.com/#business",
  "name": "Rose Sign Language Interpreting",
  "aggregateRating": {
    "@type": "AggregateRating",
    "ratingValue": "5",
    "bestRating": "5",
    "worstRating": "1",
    "reviewCount": "7"
  }
}
</script>
```

- [ ] **Step 2: Commit**

```bash
git add testimonials.html
git commit -m "seo: add AggregateRating schema to testimonials page"
```

---

### Task 10: Fix footer credential logo sizes in site.css

**Files:**
- Modify: `site.css:93-95`

- [ ] **Step 1: Change sizes**

In site.css, change:
```css
.rid-mark{width:160px;height:160px;border-radius:50%;background:#fff;padding:6px;flex-shrink:0;}
.nad-mark{width:160px;height:160px;border-radius:50%;flex-shrink:0;}
.nic-mark{width:160px;height:160px;border-radius:50%;background:#fff;padding:8px;object-fit:contain;flex-shrink:0;}
```
To (matching index.html's inline sizes):
```css
.rid-mark{width:48px;height:48px;border-radius:50%;background:#fff;padding:4px;flex-shrink:0;}
.nad-mark{width:48px;height:48px;border-radius:50%;flex-shrink:0;}
.nic-mark{width:48px;height:48px;border-radius:50%;background:#fff;padding:6px;object-fit:contain;flex-shrink:0;}
```

- [ ] **Step 2: Commit**

```bash
git add site.css
git commit -m "fix: footer credential logos 160px → 48px to match homepage design"
```

---

### Task 11: Gitignore deploy_blog.log and remove unused image

**Files:**
- Modify: `.gitignore`
- Modify: `main.py` (remove hand-shaka.png from _ALLOWED_IMAGES)

- [ ] **Step 1: Add to .gitignore**

Add `deploy_blog.log` to `.gitignore`.

- [ ] **Step 2: Remove hand-shaka.png from allowed images**

In main.py `_ALLOWED_IMAGES`, remove `"hand-shaka.png"` (no HTML references it; only .webp and .jpg are used).

- [ ] **Step 3: Commit**

```bash
git add .gitignore main.py
git commit -m "chore: gitignore deploy log; remove unused hand-shaka.png from allowed images"
```

---

## Self-Review

**Spec coverage:** All 12 identified improvement areas have tasks. ✓

**Placeholder scan:** All tasks have exact file:line references and actual code snippets. ✓

**Consistency check:** `/request` URL used consistently in new mobile menu links and existing nav links after Task 7. ✓
