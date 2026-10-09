// Run: node --test tests/web
import assert from 'node:assert/strict';
import test from 'node:test';

import { esc, markup, matches } from '../../web/js/dom.js';
import * as fmt from '../../web/js/format.js';
import { assignColors, COLORS } from '../../web/js/palette.js';
import { activeAt, axisStep, buildModel, findHits, lanes, sections, TTS_HOLD } from '../../web/js/timeline.js';

// --- fixtures -----------------------------------------------------------

function line(over) {
  return { index: 0, user_id: '1', name: 'A', kind: 'voice', at: 1000, until: 1002, text: 'x', position: null, position_end: null, exact: true, segments: [], ...over };
}

function doc(lines, audio = { status: 'ready', duration: 100 }, participants) {
  return {
    audio, started_at: 1000, lines,
    participants: participants ?? [
      { user_id: '1', name: 'Аня', speech_seconds: 30, voice_lines: 3, tts_lines: 0 },
      { user_id: '2', name: null, speech_seconds: 10, voice_lines: 1, tts_lines: 1 },
    ],
  };
}

// --- format ---------------------------------------------------------------

test('dates are shown in the given timezone', () => {
  const epoch = Date.UTC(2026, 9, 4, 18, 14, 2) / 1000; // 4 Oct 2026 18:14:02 UTC, a Sunday
  assert.equal(fmt.clock(epoch, 'Europe/Moscow'), '21:14:02');
  assert.equal(fmt.clock(epoch, 'UTC', false), '18:14');
  const p = fmt.dateParts(epoch, 'Europe/Moscow');
  assert.deepEqual([p.day, p.mon, p.wd, p.time, p.wdLong, p.monLong], ['04', 'окт', 'вс', '21:14', 'воскресенье', 'октября']);
  assert.equal(fmt.dateParts(epoch, 'No/Such_Zone').time, '18:14', 'a bad timezone falls back to UTC');
});

test('the date rolls over with the timezone', () => {
  const epoch = Date.UTC(2026, 9, 4, 22, 30, 0) / 1000;
  assert.equal(fmt.dateParts(epoch, 'Europe/Moscow').day, '05');
  assert.equal(fmt.dateParts(epoch, 'UTC').day, '04');
});

test('positions', () => {
  assert.equal(fmt.pos(0), '00:00');
  assert.equal(fmt.pos(450.3), '07:30');
  assert.equal(fmt.pos(3725), '1:02:05');
  assert.equal(fmt.pos(59, true), '0:00:59');
  assert.equal(fmt.posTenths(450.3), '07:30.3');
  assert.equal(fmt.pos(-5), '00:00');
  assert.equal(fmt.pos(null), '00:00');
});

test('time left and urgency', () => {
  assert.equal(fmt.left(89 * 86400 + 23 * 3600 + 11 * 60), '89 д 23 ч');
  assert.equal(fmt.left(5 * 3600 + 12 * 60), '5 ч 12 мин');
  assert.equal(fmt.left(12 * 60), '12 мин');
  assert.equal(fmt.left(10), '<1 мин');
  assert.equal(fmt.left(-5), '<1 мин');
  assert.equal(fmt.urgency(2 * 86400), 'crit');
  assert.equal(fmt.urgency(7 * 86400), 'warn');
  assert.equal(fmt.urgency(30 * 86400), 'ok');
  assert.equal(fmt.urgency(null), 'ok');
  assert.deepEqual(fmt.split(90061), { d: 1, h: 1, m: 1, s: 1 });
});

test('plurals, initials, names', () => {
  const forms = [1, 2, 5, 11, 12, 21, 22, 25].map((n) => fmt.plural(n, 'день', 'дня', 'дней'));
  assert.deepEqual(forms, ['день', 'дня', 'дней', 'дней', 'дней', 'день', 'дня', 'дней']);
  assert.equal(fmt.initials('Тётя Валя'), 'ТВ');
  assert.equal(fmt.initials('KEKWait'), 'KE');
  assert.equal(fmt.initials('Sgt Lomzik'), 'SL');
  assert.equal(fmt.initials(null, '123456'), '56');
  assert.equal(fmt.displayName(null, '123456'), '#3456');
  assert.equal(fmt.displayName('Аня', '1'), 'Аня');
});

// --- escaping and search --------------------------------------------------

test('esc and markup never let markup through', () => {
  assert.equal(esc('<img src=x onerror="a()"> & \'q\''), '&lt;img src=x onerror=&quot;a()&quot;&gt; &amp; &#39;q&#39;');
  assert.equal(markup('Пицца и пиццерия', 'пицц'), '<mark>Пицц</mark>а и <mark>пицц</mark>ерия');
  assert.equal(markup('<b>пицца</b>', 'пицца'), '&lt;b&gt;<mark>пицца</mark>&lt;/b&gt;');
  assert.equal(markup('a.b (c)', '(c)'), 'a.b <mark>(c)</mark>', 'regex characters in the query are literal');
  assert.equal(markup('text', ''), 'text');
  assert.equal(markup(null, 'x'), '');
  assert.equal(matches('Пицца', ' ПИЦЦ '), true);
  assert.equal(matches('Пицца', ''), false);
});

// --- palette ----------------------------------------------------------------

test('colours are stable and unique inside a session', () => {
  const ids = ['4491', '4416', '1030', '9999', '1234', '5678', '4321', '8765'];
  const a = assignColors(ids);
  assert.equal(new Set(a.values()).size, 8, 'eight people get eight different colours');
  assert.deepEqual([...assignColors(ids)], [...a], 'deterministic');
  assert.equal(assignColors(['4491']).get('4491'), assignColors(['4491', '4416']).get('4491'), 'a person keeps their colour when alone');
  assert.ok(COLORS.includes(assignColors(['x']).get('x')));
  assert.equal(assignColors([...ids, 'a', 'b']).size, 10, 'more people than colours still works');
});

// --- timeline ----------------------------------------------------------------

function sample() {
  return buildModel(doc([
    line({ index: 0, user_id: '1', text: 'Привет. Как дела?', position: 0, position_end: 5, segments: [
      { at: 1000, until: 1002, text: 'Привет.', position: 0, position_end: 2 },
      { at: 1003, until: 1005, text: 'Как дела?', position: 2.5, position_end: 5 }] }),
    line({ index: 1, user_id: '2', name: null, text: 'Нормально', position: 5.5, position_end: 8, segments: [
      { at: 1006, until: 1008, text: 'Нормально', position: 5.5, position_end: 8 }] }),
    line({ index: 2, user_id: '1', kind: 'tts', text: 'плюс', position: 6, position_end: null, exact: false, segments: [
      { at: 1007, until: 1007, text: 'плюс', position: 6, position_end: null }] }),
    line({ index: 3, user_id: '1', text: 'Потом', position: 9.5, position_end: 12, segments: [] }),
  ]));
}

test('model: speakers, colours, lines, positions', () => {
  const m = sample();
  assert.equal(m.speakers.length, 2);
  assert.equal(m.speakers[1].label, '#2', 'unknown name falls back to the id');
  assert.notEqual(m.speakers[0].color, m.speakers[1].color);
  assert.equal(m.speakers[0].speech, 30);
  assert.equal(m.hasPositions, true);
  assert.equal(m.duration, 100);
  assert.equal(m.lines[0].segs.length, 2);
  assert.equal(m.lines[3].segs.length, 1, 'a line without segments gets one made of itself');
  assert.equal(m.lines[3].pos, 9.5);
  assert.equal(m.lines[2].end, 6, 'TTS has no length of its own');
});

test('model: a user missing from participants is added from the lines', () => {
  const m = buildModel(doc([line({ user_id: '77', name: 'Гость', position: 1, position_end: 2 })]));
  assert.equal(m.speakers.at(-1).label, 'Гость');
});

test('model: without audio nothing is seekable', () => {
  const m = buildModel(doc([line({ position: 3, position_end: 4 })], { status: 'none', duration: null }));
  assert.equal(m.hasPositions, false);
  assert.equal(m.lines[0].pos, null);
  assert.equal(activeAt(m, 3.5), null);
  assert.deepEqual(m.timeline, []);
});

test('activeAt: follows the playing segment and holds during pauses', () => {
  const m = sample();
  const at = (t) => { const a = activeAt(m, t); return a && `${a.line}:${a.seg}`; };
  assert.equal(at(-1), null);
  assert.equal(at(0), '0:0');
  assert.equal(at(1.9), '0:0');
  assert.equal(at(2.2), '0:0', 'pause between two segments keeps the first');
  assert.equal(at(2.5), '0:1');
  assert.equal(at(5.2), '0:1');
  assert.equal(at(5.5), '1:0');
  assert.equal(at(99), '3:0', 'after the last segment the last stays');
});

test('activeAt: TTS takes over briefly, then the voice line is current again', () => {
  const m = sample();
  const at = (t) => activeAt(m, t).line;
  assert.equal(at(6), 2);
  assert.equal(at(6 + TTS_HOLD - 0.1), 2);
  assert.equal(at(6 + TTS_HOLD), 1, 'after the hold the interrupted voice line is current again');
  assert.equal(at(9.5), 3);
});

test('activeAt: progress inside the segment and the line', () => {
  const m = sample();
  const a = activeAt(m, 1);
  assert.equal(a.frac, 0.5);
  assert.ok(Math.abs(a.lineFrac - 1 / 4.5) < 1e-9);
  const b = activeAt(m, 3.75); // half through the second segment (2.5..5)
  assert.equal(b.frac, 0.5);
  assert.ok(Math.abs(b.lineFrac - (2 + 1.25) / 4.5) < 1e-9);
  assert.equal(activeAt(m, 5.2).frac, 1, 'in the pause after a segment it is complete');
});

test('activeAt: lines out of mix order are found by position, not by index', () => {
  // line 0 (time order first) plays after line 1 in the mix
  const m = buildModel(doc([
    line({ index: 0, user_id: '1', text: 'a', position: 10, position_end: 14 }),
    line({ index: 1, user_id: '2', text: 'b', position: 5, position_end: 9 }),
  ]));
  assert.equal(activeAt(m, 6).line, 1);
  assert.equal(activeAt(m, 11).line, 0);
});

test('lanes: merged blocks per speaker, TTS as ticks', () => {
  const m = buildModel(doc([
    line({ index: 0, user_id: '1', position: 0, position_end: 2, segments: [] }),
    line({ index: 1, user_id: '1', position: 2.4, position_end: 4, segments: [] }),
    line({ index: 2, user_id: '1', position: 20, position_end: 22, segments: [] }),
    line({ index: 3, user_id: '2', kind: 'tts', position: 10, position_end: null, segments: [] }),
  ]));
  const l = lanes(m);
  assert.deepEqual(l[0].blocks, [[0, 4], [20, 22]]);
  assert.deepEqual(l[1].ticks, [10]);
  assert.deepEqual(l[1].blocks, []);
});

test('sections: five-minute groups that never repeat', () => {
  const m = buildModel(doc([
    line({ index: 0, position: 10, position_end: 20, segments: [] }),
    line({ index: 1, position: 290, position_end: 300, segments: [] }),
    line({ index: 2, position: 310, position_end: 320, segments: [] }),
    line({ index: 3, position: 295, position_end: 299, segments: [] }), // out of order: stays with the section it follows
    line({ index: 4, position: 700, position_end: 710, segments: [] }),
  ], { status: 'ready', duration: 720 }));
  const s = sections(m, { step: 300 });
  assert.deepEqual(s.map((x) => [x.start, x.lines]), [[0, [0, 1]], [300, [2, 3]], [600, [4]]]);
  assert.equal(s[2].end, 720, 'the last section ends with the audio');
  assert.equal(s[0].share[0], 20, 'time spoken per speaker');
});

test('sections without audio use the wall clock', () => {
  const m = buildModel(doc([line({ index: 0, at: 1010 }), line({ index: 1, at: 1000 + 301 })], { status: 'none' }));
  assert.deepEqual(sections(m, { step: 300, startedAt: 1000 }).map((x) => x.lines), [[0], [1]]);
});

test('findHits and axisStep', () => {
  const m = buildModel(doc([line({ index: 0, text: 'Вкусная ПИЦЦА' }), line({ index: 1, text: 'нет' }), line({ index: 2, text: 'пиццерия' })]));
  assert.deepEqual(findHits(m, 'пицц'), [0, 2]);
  assert.deepEqual(findHits(m, ''), []);
  assert.equal(axisStep(1570), 300);
  assert.equal(axisStep(60), 15);
  assert.equal(axisStep(33000), 7200);
  assert.equal(axisStep(10 ** 9), 14400);
});
