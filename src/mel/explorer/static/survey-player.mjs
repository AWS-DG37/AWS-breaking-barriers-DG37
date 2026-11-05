import {el} from './dom.mjs';
import {englishSurvey} from './demo-language.mjs';

let stopActiveAudio=()=>{};

// translation: optional {language, items} with the same item ids and option values (the survey
// agents send the verifier's English reading). Without it, known wording comes from demo-language.mjs.
export function mountSurveyPlayer(host, {survey, session, photoUrl, onQuestionChange=()=>{}, translation=null}) {
  stopActiveAudio();
  const english=translation?.items?.length && translation.language?.split('-')[0]==='en'
    ? {...survey, language:translation.language, items:translation.items} : englishSurvey(survey);
  if(!english) session.englishText=false;
  const copyFor=language=>language?.split('-')[0]==='hi' ? {
    feedback:'आपकी राय', intro:'आपका अनुभव जानना चाहते हैं', description:'कुछ छोटे सवालों के जवाब दें। यह केवल पूर्वावलोकन है। जवाब भेजे नहीं जाते।',
    start:'शुरू करें', back:'पीछे', next:'आगे', finish:'पूरा करें', thanks:'धन्यवाद', complete:'पूर्वावलोकन पूरा हुआ। जवाब केवल इस पेज पर हैं, भेजे नहीं गए हैं।', restart:'फिर से शुरू करें', required:'आगे बढ़ने के लिए एक जवाब चुनें।', missing:'कृपया बाकी सवालों के जवाब भी दें।'
  } : {
    feedback:'Your feedback', intro:'Tell us about your experience', description:'Answer a few short questions. This is a preview. Answers are not submitted.',
    start:'Start', back:'Back', next:'Next', finish:'Finish', thanks:'Thank you', complete:'Preview complete. Answers remain on this page and have not been submitted.', restart:'Restart', required:'Choose an answer to continue.', missing:'Please answer the remaining questions too.'
  };
  const followUpFor=item=>{
    if(!Object.hasOwn(session.answers,item.item_id)) return null;
    const value=session.answers[item.item_id];
    return item.type==='survey-scale'
      ? item.follow_up_at_or_below!=null && value<=item.follow_up_at_or_below && item.follow_up_reason
      : item.options?.find(option=>option.value===value)?.follow_up;
  };
  const followUpCopy=language=>language?.split('-')[0]==='hi'
    ? 'इस जवाब पर आगे संपर्क की ज़रूरत है। यह केवल पूर्वावलोकन है; कोई संपर्क निर्धारित नहीं किया गया है।'
    : 'This answer needs follow-up. This is a preview; no contact has been scheduled.';
  const completionCopy=language=>copyFor(language).complete
    + (survey.items.some(followUpFor) ? ` ${followUpCopy(language)}` : '');
  let copy=copyFor(survey.language), displayed=survey, utterance=null;
  const stopAudio=()=>{
    if(utterance) globalThis.speechSynthesis?.cancel();
    utterance=null;
    readButton.textContent='Read aloud';
    readButton.setAttribute('aria-pressed','false');
  };
  stopActiveAudio=stopAudio;
  function readQuestion() {
    stopAudio();
    const spoken=session.englishText ? english : survey;
    const item=spoken.items[session.question];
    const spokenCopy=copyFor(spoken.language);
    const hindi=spoken.language?.split('-')[0]==='hi';
    const choices=item.options ? item.options.map(option=>option.label).join(hindi?'। ':'. ')
      : hindi ? `${item.scale_min} से ${item.scale_max} तक कोई नंबर छूएँ।` : `Tap a number from ${item.scale_min} to ${item.scale_max}.`;
    const text=session.phase==='intro' ? `${spokenCopy.intro}. ${spokenCopy.description}`
      : session.phase==='complete' ? `${spokenCopy.thanks}. ${completionCopy(spoken.language)}` : `${item.prompt} ${choices}`;
    const line=new SpeechSynthesisUtterance(text);
    line.lang=spoken.language;
    utterance=line;
    readButton.textContent='Stop voice';
    readButton.setAttribute('aria-pressed','true');
    line.onend=line.onerror=()=>{if(utterance===line) stopAudio();};
    globalThis.speechSynthesis.speak(line);
  }
  const nativeEnglish=survey.language?.split('-')[0]==='en';
  const readButton=el('button',{type:'button',class:'read-aloud','aria-pressed':'false',disabled:!globalThis.speechSynthesis,onClick:()=>utterance?stopAudio():readQuestion()},'Read aloud');
  const controls=el('div',{class:'player-demo',lang:'en'},readButton,
    !globalThis.speechSynthesis?el('p',{},'Audio is unavailable in this browser.'):null);
  // The language switch on the phone sets the language of both the text and the voice.
  const languageSwitch=english && !nativeEnglish ? el('div',{class:'language-switch',role:'group','aria-label':'Survey language'},
    [[false,'हिंदी','hi'],[true,'English','en']].map(([on,label,lang])=>el('button',{type:'button',lang,'aria-pressed':String(Boolean(session.englishText)===on),onClick:()=>{
      if(Boolean(session.englishText)===on) return;
      const reading=Boolean(utterance);
      stopAudio();
      session.englishText=on; syncSwitch(); render(true);
      if(reading) readQuestion();
    }},label))) : null;
  function syncSwitch() { languageSwitch?.querySelectorAll('button').forEach((b,i)=>b.setAttribute('aria-pressed',String(Boolean(session.englishText)===(i===1)))); }
  const phone=el('form',{onSubmit:event=>event.preventDefault(),class:'survey-phone',role:'region','aria-label':'FarmerChat survey preview',lang:survey.language || 'en'});
  const body=el('div',{class:'player-body'}), footer=el('div',{class:'player-navigation'});
  phone.append(el('div',{class:'phone-screen'},
    el('div',{class:'phone-status','aria-hidden':true},el('span',{},'9:41'),el('span',{class:'phone-camera'}),el('span',{class:'phone-reception'},el('i'),el('i'),el('i'),el('i'),el('span',{class:'phone-battery'}))),
    el('div',{class:'phone-brand'},el('strong',{},'FarmerChat'),languageSwitch || el('span',{},'Digital Green')),
    el('img',{class:'phone-field',src:photoUrl,alt:'Mustard flowers in a field in Rajasthan, India',width:960,height:768}),
    body,footer,el('div',{class:'phone-home','aria-hidden':true})));
  host.replaceChildren(controls,phone);
  const lifecycle=new AbortController();
  window.addEventListener('pagehide',stopAudio,{signal:lifecycle.signal});
  window.addEventListener('hashchange',stopAudio,{signal:lifecycle.signal});
  const removal=new MutationObserver(()=>{if(!host.isConnected) destroy();});
  removal.observe(document.body,{childList:true,subtree:true});
  function destroy() {
    stopAudio();
    if(stopActiveAudio===stopAudio) stopActiveAudio=()=>{};
    lifecycle.abort();
    removal.disconnect();
  }
  const answered=item=>Object.hasOwn(session.answers,item.item_id);
  const button=(label,action,disabled=false)=>el('button',{type:'button',onClick:action,disabled},label);
  function showQuestion(index) {
    stopAudio();
    session.question=Math.max(0,Math.min(index,survey.items.length-1));
    session.phase='questions';
    render(true);
    onQuestionChange(session.question);
  }
  function reset() {
    stopAudio();
    Object.assign(session,{phase:'intro',question:0,answers:{}});
    render(true);
    onQuestionChange(0);
  }
  function render(focus=false, message='') {
    displayed=session.englishText ? english : survey;
    copy=copyFor(displayed.language);
    phone.lang=displayed.language || 'en';
    readButton.disabled=!globalThis.speechSynthesis;
    body.replaceChildren();
    footer.replaceChildren();
    let heading;
    if(session.phase==='intro') {
      heading=el('h3',{tabindex:-1},copy.intro);
      body.append(heading,el('p',{},copy.description));
      footer.append(button(copy.start,()=>showQuestion(session.question)));
    } else if(session.phase==='complete') {
      heading=el('h3',{tabindex:-1},copy.thanks);
      body.append(heading,el('p',{},completionCopy(displayed.language)));
      footer.append(button(copy.restart,reset));
    } else {
      const item=displayed.items[session.question];
      const last=session.question===survey.items.length-1;
      heading=el('h3',{tabindex:-1},item.prompt);
      const answers=el('div',{class:'answers',role:'radiogroup','aria-label':item.prompt,lang:displayed.language || 'en'});
      const options=item.options || Array.from({length:item.scale_max-item.scale_min+1},(_,i)=>({value:item.scale_min+i,label:String(item.scale_min+i)}));
      const next=button(last?copy.finish:copy.next,()=>{
        stopAudio();
        if(!last) return showQuestion(session.question+1);
        const missing=survey.items.findIndex(question=>!answered(question));
        if(missing!==-1) {
          session.question=missing;
          render(true,copy.missing);
          onQuestionChange(missing);
          return;
        }
        session.phase='complete';
        render(true);
      },!answered(item));
      const required=el('p',{class:'player-hint',role:'status'},answered(item)
        ? followUpFor(item)?followUpCopy(displayed.language):'' : copy.required);
      for(const option of options) {
        const radio=el('input',{type:'radio',name:`answer-${item.item_id}`,value:option.value,checked:answered(item) && session.answers[item.item_id]===option.value,onChange:()=>{
          stopAudio();
          session.answers[item.item_id]=option.value;
          next.disabled=false;
          required.textContent=followUpFor(item)?followUpCopy(displayed.language):'';
        }});
        answers.append(el('label',{},radio,el('span',{},option.label)));
      }
      body.append(el('div',{class:'survey-progress'},el('span',{},copy.feedback),el('span',{},`${session.question+1} / ${survey.items.length}`)),heading,answers,required);
      if(message) body.append(el('p',{role:'status'},message));
      footer.append(button(copy.back,()=>showQuestion(session.question-1),session.question===0),next);
    }
    body.scrollTop=0;
    if(focus) heading.focus({preventScroll:true});
  }
  render();
  return {showQuestion,reset,destroy};
}
