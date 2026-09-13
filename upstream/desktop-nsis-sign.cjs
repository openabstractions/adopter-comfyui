'use strict';
// electron-builder 26.8.1's fixed SHA256 custom-sign callback. This hook
// exports or reuses an authenticated uninstaller; Certum signing happens in
// the existing separate signer. The outer NSIS remains unsigned here.
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');

function pin(file) {
  if (fs.statSync(file).size > 64 * 1024 * 1024) throw new Error('NSIS role exceeds byte bound');
  const bytes = fs.readFileSync(file);
  return { bytes: bytes.length, sha256: crypto.createHash('sha256').update(bytes).digest('hex') };
}
function same(actual, expected) {
  return expected && Object.keys(expected).sort().join(',') === 'bytes,sha256' &&
    Number.isSafeInteger(expected.bytes) && expected.bytes > 0 &&
    /^[0-9a-f]{64}$/.test(expected.sha256) && actual.bytes === expected.bytes && actual.sha256 === expected.sha256;
}
function plain(file, directory = false) {
  if (!path.isAbsolute(file) || path.resolve(file) !== file) throw new Error('Noncanonical NSIS role path');
  let cursor = file;
  while (true) {
    const info = fs.lstatSync(cursor);
    if (info.isSymbolicLink()) throw new Error('Linked NSIS role path');
    if (cursor === file && (directory ? !info.isDirectory() : !info.isFile() || info.nlink !== 1)) {
      throw new Error('Invalid NSIS role member');
    }
    const parent = path.dirname(cursor);
    if (parent === cursor) break;
    cursor = parent;
  }
  return file;
}
function context() {
  const file = plain(process.env.OA_NSIS_CONTEXT || '');
  if (fs.statSync(file).size > 16384 || pin(file).sha256 !== process.env.OA_NSIS_CONTEXT_SHA256) {
    throw new Error('NSIS source context differs');
  }
  const value = JSON.parse(fs.readFileSync(file, 'utf8'));
  const keys = ['schema','mode','source','run_id','run_attempt','recipe','output_dir','installer_name','uninstaller_name'];
  if (value.mode === 'reuse') keys.push('unsigned','signed');
  if (Object.keys(value).sort().join(',') !== keys.sort().join(',') || value.schema !== 1 ||
      !['emit','reuse'].includes(value.mode) || !/^[0-9a-f]{40}$/.test(value.source) ||
      value.source !== process.env.CANDIDATE_SHA || String(value.run_id) !== process.env.GITHUB_RUN_ID ||
      String(value.run_attempt) !== process.env.GITHUB_RUN_ATTEMPT ||
      !Number.isSafeInteger(value.run_id) || value.run_id <= 0 ||
      !Number.isSafeInteger(value.run_attempt) || value.run_attempt <= 0 ||
      !same(value.recipe, value.recipe) || value.recipe.sha256 !== process.env.OA_DESKTOP_RECIPE_SHA256 ||
      !/^OpenAbstractions-ComfyUI-[0-9]+\.[0-9]+\.[0-9]+-win-x64\.exe$/.test(value.installer_name) ||
      value.uninstaller_name !== value.installer_name.slice(0, -3) + '__uninstaller.exe') {
    throw new Error('Invalid source-bound NSIS context');
  }
  plain(value.output_dir, true);
  return value;
}

module.exports = async function oaNsisSign(configuration) {
  const value = context();
  if (!configuration || configuration.hash !== 'sha256') throw new Error('Unmatched NSIS hashing callback');
  const target = plain(configuration.path);
  if (path.dirname(target).toLowerCase() !== value.output_dir.toLowerCase()) throw new Error('Foreign NSIS output root');
  const name = path.basename(target);
  if (name === value.installer_name) {
    if (value.mode !== 'reuse') throw new Error('Outer NSIS reached before uninstaller export');
    return; // Intentionally unsigned; the API intake + sign-host owner follows.
  }
  if (name !== value.uninstaller_name) throw new Error('Unmatched NSIS native role');
  const unsigned = pin(target);
  if (value.mode === 'emit') {
    const exported = path.join(value.output_dir, 'desktop-uninstaller-unsigned.exe');
    fs.copyFileSync(target, exported, fs.constants.COPYFILE_EXCL);
    if (!same(pin(exported), unsigned)) throw new Error('NSIS export changed');
    fs.writeFileSync(path.join(value.output_dir, 'nsis-uninstaller-export.json'), JSON.stringify({
      schema: 1, source: value.source, run_id: value.run_id, run_attempt: value.run_attempt,
      recipe: value.recipe, unsigned, generated_name: name
    }) + '\n', { flag: 'wx' });
    throw new Error('OA_NSIS_UNINSTALLER_EXPORTED');
  }
  if (!same(unsigned, value.unsigned) || !value.signed ||
      Object.keys(value.signed).sort().join(',') !== 'bytes,name,sha256' ||
      value.signed.name !== 'oa-signed/desktop-uninstaller-signed.exe') throw new Error('NSIS recreation differs');
  const signed = plain(path.join(value.output_dir, ...value.signed.name.split('/')));
  const expected = { bytes: value.signed.bytes, sha256: value.signed.sha256 };
  if (!same(pin(signed), expected)) throw new Error('Signed NSIS uninstaller differs from authenticated intake');
  fs.copyFileSync(signed, target);
  if (!same(pin(target), expected) || !same(pin(signed), expected)) throw new Error('NSIS signed reuse changed');
};
