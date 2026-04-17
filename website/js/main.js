/**
 * SAG Website — Main JavaScript
 */

// Architecture tab switching
function showTab(name) {
  document.querySelectorAll('.arch-panel').forEach(function(p) {
    p.classList.remove('active');
  });
  document.querySelectorAll('.arch-tab').forEach(function(t) {
    t.classList.remove('active');
  });
  document.getElementById('p-' + name).classList.add('active');
  event.target.classList.add('active');
}

// Copy-to-clipboard for command blocks and bibtex
document.addEventListener('DOMContentLoaded', function() {
  document.querySelectorAll('.cp').forEach(function(btn) {
    btn.addEventListener('click', function() {
      var container = btn.closest('.cmd') || btn.closest('.bibtex');
      if (!container) return;

      var text;
      if (container.classList.contains('cmd')) {
        var span = container.querySelector('span');
        text = span ? span.textContent.trim() : '';
      } else {
        var clone = container.cloneNode(true);
        var btnClone = clone.querySelector('.cp');
        if (btnClone) btnClone.remove();
        text = clone.textContent.trim();
      }

      navigator.clipboard.writeText(text).then(function() {
        btn.textContent = 'COPIED';
        setTimeout(function() { btn.textContent = 'COPY'; }, 2000);
      });
    });
  });

  // Terminal typing animation
  initTerminalAnimation();
});


// ── Terminal typing animation ──────────────────────────────────────
//
// delay = pause BEFORE this line renders (simulates work happening)
// type:  "cmd"    → typed char-by-char   "output" → instant line   "pause" → blank line

var TERMINAL_LINES = [
  // User types the command
  { text: '$ sag project https://github.com/apache/commons-cli.git', cls: 'p', type: 'cmd',    delay: 500 },

  // Phase 1 — clone + analyze (~3s of "work")
  { text: '───────────────────────────────────────────',              cls: 'd', type: 'output', delay: 400 },
  { text: 'Phase 1: Project Preparing',                              cls: 'd', type: 'output', delay: 200 },
  { text: '  cloning repository...',                                 cls: '',  type: 'output', delay: 300 },
  { text: '  analyzing project structure',                           cls: '',  type: 'output', delay: 1800 },
  { text: '  detected: Java/Maven (JDK 11+)',                        cls: '',  type: 'output', delay: 800 },
  { text: '',                                                        cls: '',  type: 'pause',  delay: 300 },

  // Phase 2 — dependency resolution (~4s, resolver takes time)
  { text: 'Phase 2: Dependency Resolving',                           cls: 'd', type: 'output', delay: 200 },
  { text: '  java version mismatch → upgrading to JDK 11',          cls: '',  type: 'output', delay: 1200 },
  { text: '  resolving transitive dependencies',                     cls: '',  type: 'output', delay: 2400 },
  { text: '  ✓ dependencies resolved',                               cls: 'g', type: 'output', delay: 400 },
  { text: '',                                                        cls: '',  type: 'pause',  delay: 300 },

  // Phase 3 — build + test (longest, ~6s)
  { text: 'Phase 3: Executing & Reporting',                          cls: 'd', type: 'output', delay: 200 },
  { text: '  mvn clean install',                                     cls: '',  type: 'output', delay: 400 },
  { text: '  BUILD SUCCESS',                                         cls: 'g', type: 'output', delay: 3500 },
  { text: '  parsing surefire-reports/*.xml',                        cls: '',  type: 'output', delay: 800 },
  { text: '  tests: 847 passed, 0 failed',                          cls: '',  type: 'output', delay: 1000 },
  { text: '',                                                        cls: '',  type: 'pause',  delay: 300 },

  // Phase 4 — report (quick wrap-up)
  { text: 'Phase 4: Report Generation',                              cls: 'd', type: 'output', delay: 200 },
  { text: '  writing setup-report-commons-cli.md',                   cls: '',  type: 'output', delay: 1000 },
  { text: '  ✓ Setup completed in 4m 23s',                           cls: 'g', type: 'output', delay: 600 },
];

var CHAR_INTERVAL  = 18;   // ms per character when typing a command
var RESTART_DELAY  = 3000; // pause before looping
var TERMINAL_VISIBLE_ROWS = 15;

function initTerminalAnimation() {
  var pre = document.getElementById('demo-pre');
  if (!pre) return;
  pre.textContent = '';
  runTerminal(pre);
}

// Cursor helpers
function addCursor(pre) {
  removeCursor(pre);
  var cur = document.createElement('span');
  cur.className = 'cursor';
  pre.appendChild(cur);
}
function removeCursor(pre) {
  var old = pre.querySelector('.cursor');
  if (old) old.remove();
}

// Keep the view pinned to the bottom.
function scrollToBottom(pre) {
  pre.scrollTop = pre.scrollHeight;
}

function trimTerminalRows(pre, maxRows) {
  var rows = pre.querySelectorAll('.terminal-row');

  while (rows.length > maxRows) {
    rows[0].remove();
    rows = pre.querySelectorAll('.terminal-row');
  }
}

function syncTerminalViewport(pre) {
  trimTerminalRows(pre, TERMINAL_VISIBLE_ROWS);
  scrollToBottom(pre);
}

function runTerminal(pre) {
  var lineIndex = 0;
  addCursor(pre);

  function nextLine() {
    if (lineIndex >= TERMINAL_LINES.length) {
      removeCursor(pre);
      setTimeout(function() {
        pre.innerHTML = '';
        lineIndex = 0;
        addCursor(pre);
        nextLine();
      }, RESTART_DELAY);
      return;
    }

    var entry = TERMINAL_LINES[lineIndex];
    lineIndex++;

    if (entry.type === 'pause') {
      // Wait, then show blank line
      setTimeout(function() {
        removeCursor(pre);
        appendBlankLine(pre);
        addCursor(pre);
        syncTerminalViewport(pre);
        nextLine();
      }, entry.delay);
      return;
    }

    // Wait the delay BEFORE rendering (simulates work)
    setTimeout(function() {
      if (entry.type === 'cmd') {
        typeCommand(pre, entry, nextLine);
      } else {
        removeCursor(pre);
        appendLine(pre, entry);
        addCursor(pre);
        syncTerminalViewport(pre);
        nextLine();
      }
    }, entry.delay);
  }

  nextLine();
}

// Type out a command character by character
function typeCommand(pre, entry, callback) {
  removeCursor(pre);
  var row = document.createElement('span');
  row.className = 'terminal-row';
  var span = document.createElement('span');
  if (entry.cls) span.className = entry.cls;
  row.appendChild(span);
  pre.appendChild(row);
  addCursor(pre);

  var chars = entry.text.split('');
  var i = 0;

  function typeNext() {
    if (i < chars.length) {
      removeCursor(pre);
      span.textContent += chars[i];
      i++;
      addCursor(pre);
      syncTerminalViewport(pre);
      setTimeout(typeNext, CHAR_INTERVAL);
    } else {
      removeCursor(pre);
      addCursor(pre);
      syncTerminalViewport(pre);
      callback();
    }
  }

  typeNext();
}

// Append a full line instantly
function appendLine(pre, entry) {
  var row = document.createElement('span');
  row.className = 'terminal-row';
  var span = document.createElement('span');
  if (entry.cls) span.className = entry.cls;
  span.textContent = entry.text;
  row.appendChild(span);
  pre.appendChild(row);
}

function appendBlankLine(pre) {
  var row = document.createElement('span');
  row.className = 'terminal-row terminal-row-blank';
  pre.appendChild(row);
}

if (typeof module !== 'undefined' && module.exports) {
  module.exports = {
    TERMINAL_VISIBLE_ROWS: TERMINAL_VISIBLE_ROWS,
    trimTerminalRows: trimTerminalRows,
  };
}
