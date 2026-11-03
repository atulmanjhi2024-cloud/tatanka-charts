// Runs web/ta.js on the bars in argv[3] for the settings in argv[4] (seconds: true for seconds bars), with the holiday
// calendar in argv[5] if given ({holidays: hello's, group}); prints {defs, out} as JSON (test_ta.py).
const fs = require('fs'), vm = require('vm');
const ctx = vm.createContext({ Intl, Math, Date, Map, Number, Object, Array, JSON, cotRunner: () => ({ step: () => [null] }) });
vm.runInContext(`${fs.readFileSync(process.argv[2], 'utf8')}\n;this.IND_DEFS = IND_DEFS; this.indClean = indClean; this.indRunner = indRunner;
this.setSessionHolidays = setSessionHolidays;`, ctx);
const bars = JSON.parse(fs.readFileSync(process.argv[3], 'utf8')), configs = JSON.parse(fs.readFileSync(process.argv[4], 'utf8'));
if (process.argv[5]) { const cal = JSON.parse(fs.readFileSync(process.argv[5], 'utf8')); ctx.setSessionHolidays(cal.holidays, cal.group); }
const out = configs.map((c) => {
  const r = ctx.indRunner(ctx.indClean(c), !!c.seconds);
  return bars.map((b, i) => { r.step(b, false); return r.step(b, true); });
});
const defs = Object.fromEntries(Object.entries(ctx.IND_DEFS).map(([k, d]) => [k, { params: d.params.map(([key, , def, lo, hi, o]) => [key, def, lo, hi, o?.choices || o?.step || 'int']), lines: d.lines.map(([key]) => key), labels: d.lines.map(([, label]) => label), name: d.name }]));
process.stdout.write(JSON.stringify({ defs, out }));
