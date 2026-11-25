/* Empacota os módulos TypeScript locais em um bundle IIFE sem binários nativos. */
const fs = require('node:fs');
const path = require('node:path');
const ts = require('typescript');

const projectRoot = path.resolve(__dirname, '..');
const sourceRoot = path.join(projectRoot, 'frontend', 'src');
const entryFile = path.join(sourceRoot, 'index.tsx');
const outputFile = path.join(projectRoot, 'dashboard', 'dist', 'index.js');
const modules = new Map();

function moduleId(file) {
  return `./${path.relative(sourceRoot, file).split(path.sep).join('/')}`
    .replace(/\.(tsx?|jsx?)$/, '');
}

function resolveModule(fromFile, specifier) {
  if (!specifier.startsWith('.')) {
    throw new Error(`Import externo não suportado pelo bundle Hermes: ${specifier}`);
  }
  const base = path.resolve(path.dirname(fromFile), specifier);
  for (const extension of ['.ts', '.tsx', '.js', '.jsx']) {
    const candidate = `${base}${extension}`;
    if (fs.existsSync(candidate) && fs.statSync(candidate).isFile()) return candidate;
  }
  throw new Error(`Módulo não encontrado: ${specifier} (importado por ${fromFile})`);
}

function collect(file) {
  const id = moduleId(file);
  if (modules.has(id)) return;

  const source = fs.readFileSync(file, 'utf8');
  const result = ts.transpileModule(source, {
    fileName: file,
    reportDiagnostics: true,
    compilerOptions: {
      target: ts.ScriptTarget.ES2020,
      module: ts.ModuleKind.CommonJS,
      jsx: ts.JsxEmit.React,
      esModuleInterop: true,
    },
  });
  const errors = (result.diagnostics || []).filter(
    (diagnostic) => diagnostic.category === ts.DiagnosticCategory.Error,
  );
  if (errors.length) {
    throw new Error(ts.formatDiagnosticsWithColorAndContext(errors, {
      getCurrentDirectory: () => projectRoot,
      getCanonicalFileName: (name) => name,
      getNewLine: () => '\n',
    }));
  }

  // Reserve the module before descending so circular imports remain safe.
  modules.set(id, result.outputText);
  for (const [, specifier] of result.outputText.matchAll(/require\(["'](\.[^"']*)["']\)/g)) {
    collect(resolveModule(file, specifier));
  }
}

collect(entryFile);

const moduleEntries = [...modules]
  .map(([id, source]) => `${JSON.stringify(id)}: function(module, exports, require) {\n${source}\n}`)
  .join(',\n');
const bundle = `(function() {\n'use strict';\nconst modules = {\n${moduleEntries}\n};\n` +
  `const cache = Object.create(null);\n` +
  `function load(id) {\n` +
  `  if (cache[id]) return cache[id].exports;\n` +
  `  const factory = modules[id];\n` +
  `  if (!factory) throw new Error('Módulo não empacotado: ' + id);\n` +
  `  const module = { exports: {} };\n` +
  `  cache[id] = module;\n` +
  `  factory(module, module.exports, (specifier) => load(resolve(id, specifier)));\n` +
  `  return module.exports;\n` +
  `}\n` +
  `function resolve(from, specifier) {\n` +
  `  const parts = from.slice(2).split('/'); parts.pop();\n` +
  `  for (const part of specifier.split('/')) {\n` +
  `    if (!part || part === '.') continue;\n` +
  `    if (part === '..') parts.pop(); else parts.push(part);\n` +
  `  }\n` +
  `  return './' + parts.join('/');\n` +
  `}\n` +
  `load(${JSON.stringify(moduleId(entryFile))});\n` +
  `})();\n`;

fs.mkdirSync(path.dirname(outputFile), { recursive: true });
fs.writeFileSync(outputFile, bundle, 'utf8');
console.log(`Bundle gerado: ${path.relative(projectRoot, outputFile)} (${modules.size} módulos)`);
