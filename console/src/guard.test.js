// ND-visual: quarantine-the-script guard, mirroring the discipline
// tests/test_console_theme.py already established for the Streamlit
// dashboard's own CSS pass (see that file's
// test_console_css_sidebar_gets_a_dark_rail_treatment_without_mockup_copy).
// The Stitch mockups this pass ported layout/spacing/typography FROM
// (/Users/ck/Downloads/stitch_alphaos_operator_console/, outside this repo)
// contain known-fabricated content from earlier in this project's history:
// a fake operator identity ("CONSOLE_01" / "OPERATOR_ACTIVE"), and futures
// tickers ("NQ1!" / "ES1!") that don't exist anywhere in this system. This
// test does not scan the mockups themselves (not part of this repo) -- it
// asserts those specific strings, and any external URL beyond the known-
// inert baseline, never made it INTO this codebase's own source or build
// output.
import { describe, expect, it } from 'vitest';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const SRC_ROOT = path.dirname(fileURLToPath(import.meta.url)); // console/src
const DIST_DIR = path.resolve(SRC_ROOT, '../dist');

const BANNED_STRINGS = ['CONSOLE_01', 'OPERATOR_ACTIVE', 'NQ1!', 'ES1!'];

// Known-inert baseline: URL substrings allowed in source/build output
// because they're either this app's own documented deep link, or an inert
// namespace/reference URI bundled by a dependency -- never a live
// browser-side call this app itself makes. A match here does NOT assert a
// network request happens; it names the specific strings this pass
// verified are safe, so nothing new can slip in unnoticed.
const ALLOWED_URL_SUBSTRINGS = [
  'localhost:8502',  // ND-3's own documented Streamlit deep-link (api.js STREAMLIT_URL)
  'w3.org',           // W3C namespace URIs (SVG/XHTML/MathML/XML) -- inert markup, not network calls
  'react.dev',        // React 19's own bundled minified-error reference URL
  'reactjs.org',       // older React internals still reference this error-decoder host
];

function walk(dir, exts, out = []) {
  if (!fs.existsSync(dir)) return out;
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) {
      walk(full, exts, out);
    } else if (exts.some((e) => entry.name.endsWith(e))) {
      out.push(full);
    }
  }
  return out;
}

function readAll(files) {
  return files.map((f) => ({ file: f, text: fs.readFileSync(f, 'utf8') }));
}

const URL_RE = /\bhttps?:\/\/[^\s"'`)]+/g;
function urlsIn(text) {
  return [...new Set(text.match(URL_RE) ?? [])];
}

function offendingUrls(entries) {
  const offenders = [];
  for (const { file, text } of entries) {
    for (const url of urlsIn(text)) {
      if (!ALLOWED_URL_SUBSTRINGS.some((allowed) => url.includes(allowed))) {
        offenders.push(`${file}: ${url.slice(0, 160)}`);
      }
    }
  }
  return offenders;
}

describe('quarantine-the-script guard (ND-visual pass) -- console/src', () => {
  const srcFiles = walk(SRC_ROOT, ['.jsx', '.js']).filter((f) => !f.endsWith('.test.js'));

  it('has source files to scan (sanity check the walk itself works)', () => {
    expect(srcFiles.length).toBeGreaterThan(5);
  });

  // ND-6: the walk is recursive (walk() above calls itself into every
  // subdirectory) so it automatically covers new surfaces without a
  // per-directory allowlist -- this sanity check pins that down explicitly
  // for the new components/hooks added this pass (Masthead, InstrumentBlock,
  // StatTile, Funnel, Sparkline, hooks/useAnnunciator), so a future refactor
  // of walk() that accidentally stops recursing gets caught here.
  it('the recursive walk covers the ND-6 components/ and hooks/ additions', () => {
    const names = srcFiles.map((f) => path.basename(f));
    for (const expected of ['Masthead.jsx', 'InstrumentBlock.jsx', 'StatTile.jsx', 'Funnel.jsx', 'Sparkline.jsx', 'useAnnunciator.js']) {
      expect(names).toContain(expected);
    }
  });

  for (const banned of BANNED_STRINGS) {
    it(`never contains the fabricated mockup string "${banned}"`, () => {
      const hits = readAll(srcFiles).filter(({ text }) => text.includes(banned)).map((h) => h.file);
      expect(hits).toEqual([]);
    });
  }

  it('every external URL literal is on the known-inert baseline', () => {
    expect(offendingUrls(readAll(srcFiles))).toEqual([]);
  });
});

describe('quarantine-the-script guard (ND-visual pass) -- console/dist (post-build)', () => {
  const distExists = fs.existsSync(DIST_DIR);
  const distFiles = distExists ? walk(DIST_DIR, ['.js', '.html', '.css']) : [];

  it.skipIf(!distExists)('has a built dist/ to scan', () => {
    expect(distFiles.length).toBeGreaterThan(0);
  });

  for (const banned of BANNED_STRINGS) {
    it.skipIf(!distExists)(`never contains the fabricated mockup string "${banned}"`, () => {
      const hits = readAll(distFiles).filter(({ text }) => text.includes(banned)).map((h) => h.file);
      expect(hits).toEqual([]);
    });
  }

  it.skipIf(!distExists)('every external URL literal is on the known-inert baseline', () => {
    expect(offendingUrls(readAll(distFiles))).toEqual([]);
  });
});
