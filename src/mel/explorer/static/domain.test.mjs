import test from 'node:test';
import assert from 'node:assert/strict';
import {parseJsonLines, questionAssessments, displayValue, eventsForStage} from './domain.mjs';

test('JSONL retains records and identifies a bad line before submission', () => {
  assert.deepEqual(parseJsonLines('\n{"insight_id":"a"}\n{"insight_id":"b"}'), [{insight_id:'a'}, {insight_id:'b'}]);
  assert.throws(() => parseJsonLines('{}\nnot json'), /Line 2/);
  assert.throws(() => parseJsonLines('[1]'), /object/);
  assert.deepEqual(parseJsonLines(''), []);
});

test('missing scores remain unknown while zero is a measured value', () => {
  assert.equal(displayValue(null), 'Not recorded');
  assert.equal(displayValue(0), '0');
  assert.equal(displayValue(false), 'false');
});

test('question quality belongs to the exact run, insight and item', () => {
  const assessments = [
    {scope:'survey',run_id:'r1',insight_id:'i1'},
    {scope:'question',run_id:'r1',insight_id:'i1',item_id:'q1'},
    {scope:'question',run_id:'r2',insight_id:'i1',item_id:'q1'},
    {scope:'question',run_id:'r1',insight_id:'i2',item_id:'q1'},
    {scope:'question',run_id:'r1',insight_id:'i1',item_id:'q2'},
  ];
  assert.deepEqual(questionAssessments(assessments, 'r1', 'i1', 'q1'), [assessments[1]]);
});

test('stage history preserves actual revisions without synthesizing events', () => {
  const events=[{stage:'generate',sequence:1},{stage:'review',sequence:2},{stage:'generate',sequence:3}];
  assert.deepEqual(eventsForStage(events,'generate'), [events[0],events[2]]);
  assert.deepEqual(eventsForStage([], 'generate'), []);
});
