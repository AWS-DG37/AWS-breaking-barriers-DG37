import test from 'node:test';
import assert from 'node:assert/strict';
import {questionAssessments, displayValue, eventsForStage, lineDiff, changedPaths, read, write} from './domain.mjs';

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

test('line diff keeps shared lines and marks removals and additions', () => {
  const diff = lineDiff('a\nb\nc', 'a\nB\nc\nd');
  assert.deepEqual(diff.map(d => d.op + d.text), [' a', '-b', '+B', ' c', '+d']);
  assert.deepEqual(lineDiff('same', 'same'), [{op:' ', text:'same'}]);
});

test('changed paths compare nested values, and write creates missing objects', () => {
  const before = {config:{graph:{max_revisions:1}, review:{model_id:'a'}}};
  const after = structuredClone(before);
  write(after, 'config.graph.max_revisions', 2);
  write(after, 'config.batch.concurrency', 3);
  assert.equal(read(after, 'config.batch.concurrency'), 3);
  assert.deepEqual(changedPaths(before, after, ['config.graph.max_revisions', 'config.review.model_id', 'config.batch.concurrency']),
    ['config.graph.max_revisions', 'config.batch.concurrency']);
});
