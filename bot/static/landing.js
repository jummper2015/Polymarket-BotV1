/* ============================================================================
   bot/static/landing.js
   Scroll-triggered reveal animations for the landing page.
   Uses IntersectionObserver; falls back to immediate visibility on
   legacy browsers. Honors `prefers-reduced-motion: reduce`.
   ============================================================================ */
(function () {
  "use strict";

  var REDUCED = window.matchMedia &&
               window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  // No-op path when reduced motion is on or IO is unavailable.
  if (REDUCED || !("IntersectionObserver" in window)) {
    document.querySelectorAll("[data-reveal]").forEach(function (el) {
      el.classList.add("is-visible");
    });
    return;
  }

  // Stagger children inside a [data-reveal-stagger] container.
  function setupStagger(container, baseDelay) {
    var children = container.querySelectorAll("[data-reveal-child]");
    children.forEach(function (child, i) {
      // 70ms per child, capped so a long list doesn't take >600ms total.
      child.style.transitionDelay = Math.min(i * 70, 600) + "ms";
    });
  }

  // Reveal single element.
  function reveal(el) {
    el.classList.add("is-visible");
  }

  // One observer for everything; cheap.
  var observer = new IntersectionObserver(function (entries) {
    entries.forEach(function (entry) {
      if (!entry.isIntersecting) return;
      var el = entry.target;
      // For staggered groups, animate the children in order.
      if (el.hasAttribute("data-reveal-stagger")) {
        setupStagger(el, 0);
        el.querySelectorAll("[data-reveal-child]").forEach(reveal);
      }
      reveal(el);
      observer.unobserve(el);
    });
  }, {
    threshold: 0.12,        // 12% visible before firing
    rootMargin: "0px 0px -8% 0px", // start slightly before fully on-screen
  });

  // Wire up.
  function init() {
    document
      .querySelectorAll("[data-reveal], [data-reveal-stagger]")
      .forEach(function (el) { observer.observe(el); });
  }

  // Smooth scroll for in-page anchor links (CSS scroll-behavior is also set
  // as a progressive enhancement, but this handles older browsers).
  document.addEventListener("click", function (e) {
    var a = e.target.closest('a[href^="#"]');
    if (!a) return;
    var id = a.getAttribute("href").slice(1);
    if (!id) return;
    var target = document.getElementById(id);
    if (!target) return;
    e.preventDefault();
    target.scrollIntoView({ behavior: "smooth", block: "start" });
    history.replaceState(null, "", "#" + id);
  });

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
