import assert from 'node:assert/strict';
import {readFileSync, readdirSync} from 'node:fs';
import {test} from 'node:test';
import {englishSurvey} from './demo-language.mjs';

const root=new URL('../../../../',import.meta.url);
const read=path=>JSON.parse(readFileSync(new URL(path,root),'utf8'));
const generated=read('docs/voice/generated/index.json').map(entry=>read(`docs/voice/generated/${entry.file}`));

test('English demo preserves survey identity, answer values, and follow-up behavior',()=>{
  for(const survey of generated) {
    const original=structuredClone(survey);
    const translated=englishSurvey(survey);
    assert.equal(translated.language,'en-IN');
    assert.equal(translated.items[0].prompt,'Did you spray the recommended insecticide?');
    assert.equal(translated.items[0].options[0].label,'Yes');
    const canonical=structuredClone(translated);
    canonical.language=survey.language;
    canonical.items.forEach((item,i)=>{
      item.prompt=survey.items[i].prompt;
      item.options?.forEach((option,j)=>{option.label=survey.items[i].options[j].label;});
    });
    assert.deepEqual(canonical,survey);
    assert.deepEqual(survey,original);
  }
});

test('all reference survey questions and labels match their paired English version',()=>{
  const scenarios=new URL('docs/participant-pack/json/scenarios/',root);
  for(const name of readdirSync(scenarios)) {
    if(name.startsWith('6-')) continue;
    const hindi=read(`docs/participant-pack/json/scenarios/${name}/survey.hi-IN.json`);
    const english=read(`docs/participant-pack/json/scenarios/${name}/survey.en-IN.json`);
    const translated=englishSurvey(hindi);
    translated.items.forEach((item,i)=>{
      assert.equal(item.prompt,english.items[i].prompt);
      assert.deepEqual(item.options?.map(o=>o.label),english.items[i].options?.map(o=>o.label));
    });
  }
});

test('changed prompts, changed choices, and unsupported languages never borrow a demo translation',()=>{
  for(const change of [
    survey=>{survey.items[0].prompt+=' नया';},
    survey=>{survey.items[0].options[0].label='नया जवाब';},
    survey=>{survey.language='bn-IN';},
  ]) {
    const survey=structuredClone(generated[0]);
    change(survey);
    assert.equal(englishSurvey(survey),null);
  }
});

test('English surveys use their original wording',()=>{
  const survey=read('docs/participant-pack/json/scenarios/1-mustard-aphid-spray/survey.en-IN.json');
  assert.equal(englishSurvey(survey),survey);
});
