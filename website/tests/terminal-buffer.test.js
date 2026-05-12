const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

function loadMainScript() {
  const source = fs.readFileSync(path.join(__dirname, '..', 'js', 'main.js'), 'utf8');
  const context = {
    document: {
      addEventListener() {},
      getElementById() { return null; },
      querySelectorAll() { return []; },
    },
    event: { target: { classList: { add() {} } } },
    module: { exports: {} },
    navigator: { clipboard: { writeText() { return Promise.resolve(); } } },
    setTimeout() {},
  };

  vm.runInNewContext(source, context);
  return context.module.exports;
}

function createTerminalRows(labels) {
  const pre = { rows: [] };
  pre.querySelectorAll = function(selector) {
    return selector === '.terminal-row' ? this.rows.slice() : [];
  };

  pre.rows = labels.map(function(label) {
    return {
      label,
      remove() {
        pre.rows = pre.rows.filter(function(row) {
          return row.label !== label;
        });
      },
    };
  });

  return pre;
}

test('trimTerminalRows removes old rows from the top', () => {
  const { trimTerminalRows } = loadMainScript();
  const pre = createTerminalRows(['one', 'two', 'three', 'four', 'five']);

  trimTerminalRows(pre, 3);

  assert.deepEqual(pre.rows.map((row) => row.label), ['three', 'four', 'five']);
});
