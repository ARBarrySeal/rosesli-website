// Nav toggle
function toggleNav(btn) {
  const links = document.querySelector('.nav-links');
  const expanded = btn.getAttribute('aria-expanded') === 'true';
  btn.setAttribute('aria-expanded', String(!expanded));
  btn.setAttribute('aria-label', expanded ? 'Open menu' : 'Close menu');
  links.classList.toggle('open');
  btn.classList.toggle('is-open');
}

// Close nav on outside click
document.addEventListener('click', (e) => {
  const links = document.querySelector('.nav-links');
  const toggle = document.querySelector('.nav-toggle');
  if (links && links.classList.contains('open') && !e.target.closest('nav')) {
    links.classList.remove('open');
    toggle.classList.remove('is-open');
    toggle.setAttribute('aria-expanded', 'false');
    toggle.setAttribute('aria-label', 'Open menu');
  }
});

// Close nav on Escape
document.addEventListener('keydown', (e) => {
  if (e.key !== 'Escape') return;
  const links = document.querySelector('.nav-links');
  const toggle = document.querySelector('.nav-toggle');
  if (links && links.classList.contains('open')) {
    links.classList.remove('open');
    toggle.classList.remove('is-open');
    toggle.setAttribute('aria-expanded', 'false');
    toggle.setAttribute('aria-label', 'Open menu');
    toggle.focus();
  }
});

// Scroll animations — cards, stats, testimonials, client logos
const scrollObserver = new IntersectionObserver((entries) => {
  entries.forEach(entry => {
    if (entry.isIntersecting) {
      entry.target.classList.add('in-view');
      scrollObserver.unobserve(entry.target);
    }
  });
}, { threshold: 0.1, rootMargin: '0px 0px -30px 0px' });

document.querySelectorAll('.card, .card-dark, .work-item, .team-card, .stat, blockquote, .client-logo').forEach(el => {
  el.classList.add('will-animate');
  // Stagger siblings of the same parent
  const siblings = Array.from(el.parentElement.children).filter(c => c.classList.contains('will-animate'));
  el.style.transitionDelay = `${siblings.indexOf(el) * 0.08}s`;
  scrollObserver.observe(el);
});

// Counter animation for stat numbers
function animateCount(el) {
  const text = el.textContent.trim();
  const match = text.match(/^(\d+)(\+|%)?$/);
  if (!match) return;
  const target = parseInt(match[1]);
  const suffix = match[2] || '';
  const duration = 1200;
  const start = performance.now();
  const tick = (now) => {
    const t = Math.min((now - start) / duration, 1);
    const ease = 1 - Math.pow(1 - t, 3); // cubic ease out
    el.textContent = Math.round(ease * target) + suffix;
    if (t < 1) requestAnimationFrame(tick);
  };
  requestAnimationFrame(tick);
}

const counterObserver = new IntersectionObserver((entries) => {
  entries.forEach(entry => {
    if (entry.isIntersecting) {
      animateCount(entry.target);
      counterObserver.unobserve(entry.target);
    }
  });
}, { threshold: 0.5 });

document.querySelectorAll('.stat .num').forEach(el => counterObserver.observe(el));

// Formspree AJAX form submission
const form = document.querySelector('form[data-form]');
if (form) {
  form.addEventListener('submit', async (e) => {
    e.preventDefault();
    const btn = form.querySelector('.btn-submit');
    const origText = btn.textContent;
    btn.textContent = 'Sending…';
    btn.disabled = true;
    try {
      const res = await fetch(form.action, {
        method: 'POST',
        body: new FormData(form),
        headers: { Accept: 'application/json' }
      });
      if (res.ok) {
        form.style.display = 'none';
        const msg = document.getElementById('success-msg');
        msg.style.display = 'block';
        msg.scrollIntoView({ behavior: 'smooth', block: 'center' });
      } else {
        throw new Error('server');
      }
    } catch {
      btn.textContent = origText;
      btn.disabled = false;
      alert('There was a problem submitting. Please email info@rosesli.com directly.');
    }
  });
}
