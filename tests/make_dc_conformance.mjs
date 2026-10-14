// make_dc_conformance.mjs -- what the datacenter viewer makes of each layout.
//
//   DCVIZ=~/datacenter_visualization node tests/make_dc_conformance.mjs
//
// Reads every tests/fixtures/dc_conformance/*.dc, parses it with the
// viewer's own parseLayout, and writes tests/fixtures/dc_conformance.json:
// each layout with the elements the viewer made of it.  That file is what
// manifest and reckon are held to (test_manifest.sh), and model_network's
// copy of the parser is held to the same file -- the viewer is the
// reference for the .dc grammar, and three Python readers had drifted from
// it before this existed.  Regenerate after the viewer's grammar changes,
// and say which viewer commit in the commit message.
//
// color= is left out: the viewer drops one it cannot paint, which is
// drawing, not structure.  A leading byte-order mark is dropped, as a
// browser's decoder drops it.

import fs from 'fs';
import path from 'path';
import { execFileSync } from 'child_process';
import { pathToFileURL } from 'url';

const here = path.dirname(new URL(import.meta.url).pathname);
const dcviz = process.env.DCVIZ;
if (!dcviz) {
  console.error('make_dc_conformance: set DCVIZ to a datacenter_visualization checkout');
  process.exit(2);
}
const { parseLayout } = await import(pathToFileURL(path.join(dcviz, 'js', 'parse.js')).href);
let commit = 'unknown';
try {
  commit = execFileSync('git', ['-C', dcviz, 'rev-parse', '--short', 'HEAD']).toString().trim();
} catch { /* not a git checkout */ }

const dir = path.join(here, 'fixtures', 'dc_conformance');
const cases = [];
for (const file of fs.readdirSync(dir).filter((f) => f.endsWith('.dc')).sort()) {
  const text = fs.readFileSync(path.join(dir, file), 'utf8').replace(/^﻿/, '');
  const model = parseLayout(text);
  const elements = model.all.map((el) => {
    // Inherited attributes sit on the prototype chain; for..in walks it.
    const keys = [];
    for (const k in el.attrsEff) if (k !== 'color') keys.push(k);
    const attrs = {};
    for (const k of keys.sort()) attrs[k] = String(el.attrsEff[k]);
    return [el.key, el.kind, String(el.id), String(el.name), [...el.tagsAll].map(String).sort(), attrs];
  });
  cases.push({ file, elements });
}

// One case to a line, so a change to the viewer shows as a diff of the
// cases it moved.
const lines = cases.map((c) => JSON.stringify(c));
fs.writeFileSync(path.join(here, 'fixtures', 'dc_conformance.json'),
  `{"reference": ${JSON.stringify(`datacenter_visualization js/parse.js @ ${commit}`)},\n`
  + `"cases": [\n${lines.join(',\n')}\n]}\n`);
console.log(`${cases.length} layouts, ${cases.reduce((n, c) => n + c.elements.length, 0)} elements, viewer @ ${commit}`);
