import test from 'node:test';
import assert from 'node:assert/strict';
import {questionAssessments, displayValue, eventsForStage, journeyVisits, lineDiff, changedPaths, read, write} from './domain.mjs';

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

test('journey preserves context before eligibility and distinct revision visits', () => {
  const events = [
    {stage:'select',attempt:0,data:{insight:{}}},
    {stage:'context',attempt:0,data:{context:{}}},
    {stage:'select',attempt:0,data:{eligible:true}},
    {stage:'generate',attempt:1,data:{kind:'request'}},
    {stage:'generate',attempt:1,data:{kind:'response'}},
    {stage:'schema',attempt:1,data:{valid:false}},
    {stage:'generate',attempt:2,data:{kind:'request'}},
    {stage:'generate',attempt:2,data:{kind:'response'}},
    {stage:'schema',attempt:2,data:{valid:true}},
    {stage:'review',attempt:2,data:{}},
    {stage:'decision',attempt:2,data:{status:'accepted'}},
  ];
  const visits = journeyVisits(events);
  assert.deepEqual(visits.map(v=>v.phase), ['input','context','select','generate','schema','generate','schema','review','decision']);
  assert.deepEqual(visits[2].eventIndices, [1]);
  assert.deepEqual(visits[5].eventIndices, [2,3]);
  assert.deepEqual(visits.flatMap(v=>v.events), events);
});

test('skipped, failed and untraced outcomes do not invent downstream stages', () => {
  assert.deepEqual(journeyVisits(), []);
  const skip = [{stage:'select',data:{eligible:false}},{stage:'decision',data:{status:'skipped'}}];
  assert.deepEqual(journeyVisits(skip).map(v=>v.stage), ['select','decision']);
  const failed = [{stage:'generate',attempt:1},{stage:'failure',attempt:1}];
  assert.deepEqual(journeyVisits(failed).map(v=>v.stage), ['generate','failure']);
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
