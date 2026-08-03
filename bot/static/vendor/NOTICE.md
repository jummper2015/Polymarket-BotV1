# Third-party assets

Vendored rather than installed via a bundler on purpose: the bot deploys to a VPS that
only needs Python. Adding Vite/npm to the build would mean adding Node to the deploy.

## Notika Admin Template

- **Source:** https://github.com/puikinsh/notika
- **Author:** [Colorlib](https://colorlib.com)
- **License:** MIT — *"Attribution to Colorlib as the original author is required."*
- **Files:** `notika/style.css`, `notika/modern.css`, `notika/css/*.css`

`modern.css` is compiled from Notika's `src/css/modern.scss`; it holds the design
tokens (`--notika-shadow`, transitions, z-index scale) that `style.css` — the
older theme sheet — doesn't define. To regenerate after updating Notika:

```bash
npx sass --no-source-map --style=compressed \
    notika/green-horizotal/src/css/modern.scss modern.css
```

Only the stylesheets are used. Notika's own HTML loads everything through Vite
(`<script type="module" src="/src/js/main.js">`) and its icons come from Font Awesome 7
via that bundle, so neither the markup nor the JS is reusable here — the templates in
`bot/templates/` are ours, written against Notika's class names.

Note that `style.css` is the theme only; it does not contain Bootstrap. That's why
Bootstrap is vendored separately below.

## Bootstrap 5.3.8

- **Source:** https://github.com/twbs/bootstrap
- **License:** MIT
- **Files:** `bootstrap/bootstrap.min.css`, `bootstrap/bootstrap.bundle.min.js`

## Bootstrap Icons 1.13.1

- **Source:** https://github.com/twbs/icons
- **License:** MIT
- **Files:** `icons/bootstrap-icons.min.css`, `icons/fonts/*`

Used instead of Notika's Font Awesome, which requires a bundler to tree-shake. The CSS
references `./fonts/`, so that directory must stay next to it.

## Chart.js 4.5.1

- **Source:** https://github.com/chartjs/Chart.js
- **License:** MIT
- **Files:** `chartjs/chart.umd.min.js`

## Updating

```bash
npm pack bootstrap@<v> chart.js@<v> bootstrap-icons@<v>
```

Then copy `dist/` (Bootstrap, Chart.js) or `font/` (Icons) into the matching directory.
Notika's stylesheets come from `notika/green-horizotal/` in its repository.
