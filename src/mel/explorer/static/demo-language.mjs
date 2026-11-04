// Bundled English demo wording, matched to the exact original prompts and labels.
// ponytail: covers the recorded demo and participant-pack surveys. New wording needs
// a translation entry; unknown text disables English controls instead of guessing.
const translations = {
  "क्या आपने सरसों पर माहू (छोटे हरे कीड़े) के लिए छिड़काव किया?": {
    "prompt": "Did you spray your mustard for the aphids (small green insects)?",
    "labels": {
      "हाँ": "Yes",
      "नहीं": "No"
    }
  },
  "क्या छिड़काव की दवा आसानी से मिल गई?": {
    "prompt": "Was it easy to get the spray?",
    "labels": {
      "हाँ, आसानी से": "Yes, easily",
      "हाँ, मुश्किल से": "Yes, with difficulty",
      "नहीं मिली": "Could not get it"
    }
  },
  "अब पौधों पर ये कीड़े कितने हैं?": {
    "prompt": "How many of these insects are on the plants now?",
    "labels": {
      "लगभग नहीं": "Almost none",
      "पहले से कम": "Fewer than before",
      "पहले जितने": "Same as before",
      "पहले से ज़्यादा": "More than before"
    }
  },
  "क्या आपने गेहूँ में पहली सिंचाई कर दी?": {
    "prompt": "Did you give the first irrigation to your wheat?",
    "labels": {
      "हाँ, समय पर": "Yes, on time",
      "हाँ, पर देर से": "Yes, but later",
      "अभी नहीं": "Not yet"
    }
  },
  "आपकी गेहूँ की फसल अब कैसी दिख रही है?": {
    "prompt": "How does your wheat crop look now?",
    "labels": {
      "हरी और स्वस्थ": "Green and healthy",
      "कुछ पत्ते पीले": "Some yellow leaves",
      "बहुत पत्ते पीले": "Many yellow leaves"
    }
  },
  "क्या आपने पिछले 3 दिनों में आलू के पत्ते देखे?": {
    "prompt": "Did you check the potato leaves in the last 3 days?",
    "labels": {
      "हाँ": "Yes",
      "नहीं": "No"
    }
  },
  "भूरे धब्बों का अब क्या हाल है?": {
    "prompt": "What is happening to the brown spots?",
    "labels": {
      "नए धब्बे नहीं": "No new spots",
      "पहले जैसे ही": "Same as before",
      "फैल रहे हैं": "Spreading",
      "सफ़ेद फफूंद दिखी": "White growth seen"
    }
  },
  "सलाह कितनी काम की रही? 1 मतलब बिल्कुल नहीं, 5 मतलब बहुत काम की।": {
    "prompt": "How useful was the advice? 1 means not useful, 5 means very useful.",
    "labels": {}
  },
  "क्या पशु डॉक्टर ने आपकी गाय को देखा?": {
    "prompt": "Did a vet see your cow?",
    "labels": {
      "हाँ": "Yes",
      "नहीं": "No"
    }
  },
  "आपकी गाय अब कैसी है?": {
    "prompt": "How is your cow now?",
    "labels": {
      "पहले से ठीक": "Better",
      "पहले जैसी": "Same as before",
      "पहले से खराब": "Worse"
    }
  },
  "आपने गेहूँ का कौन सा बीज बोया?": {
    "prompt": "Which wheat seed did you sow?",
    "labels": {
      "सुझाई गई किस्म": "The suggested type",
      "कोई दूसरी किस्म": "A different type",
      "अभी नहीं बोया": "Not sown yet"
    }
  },
  "अच्छा बीज मिलना कितना आसान था? 1 मतलब बहुत मुश्किल, 5 मतलब बहुत आसान।": {
    "prompt": "How easy was it to find good seed? 1 means very hard, 5 means very easy.",
    "labels": {}
  },
  "क्या आपने अनुशंसित कीटनाशक का छिड़काव किया?": {
    "prompt": "Did you spray the recommended insecticide?",
    "labels": {
      "हाँ": "Yes",
      "अभी नहीं": "Not yet",
      "नहीं": "No",
      "कीटनाशक नहीं मिला": "Could not get the insecticide"
    }
  },
  "सरसों के फूलों और तनों पर हरे कीड़ों की स्थिति कैसी है?": {
    "prompt": "How are the green insects on the mustard flowers and stems now?",
    "labels": {
      "कम हुए": "Fewer",
      "वैसे ही हैं": "The same",
      "बढ़ गए": "More",
      "कह नहीं सकते": "Cannot tell"
    }
  },
  "सरसों की फूलों और तनों पर छोटी हरी कीड़े (माहू) कैसी हैं?": {
    "prompt": "How are the small green insects (aphids) on the mustard flowers and stems?",
    "labels": {
      "ठीक हो गई": "Better",
      "जैसी ही है": "The same",
      "और बढ़ गई": "More",
      "नहीं जानता/जानती": "Do not know",
      "छिड़काव नहीं किया": "Did not spray"
    }
  },
  "आपने छिड़काव करते समय किस समय पर किया?": {
    "prompt": "What time of day did you spray?",
    "labels": {
      "शाम को": "In the evening",
      "दिन में": "During the day",
      "सुबह": "In the morning",
      "छिड़काव नहीं किया": "Did not spray"
    }
  }
};

export function englishSurvey(survey) {
  if (survey.language?.split('-')[0] === 'en') return survey;
  if (survey.language?.split('-')[0] !== 'hi') return null;
  const items = [];
  for (const item of survey.items) {
    const translation = Object.hasOwn(translations, item.prompt) ? translations[item.prompt] : null;
    if (!translation || item.options?.some(option => !Object.hasOwn(translation.labels, option.label))) return null;
    items.push({...item, prompt: translation.prompt, ...(item.options ? {
      options: item.options.map(option => ({...option, label: translation.labels[option.label]})),
    } : {})});
  }
  return {...survey, language: 'en-IN', items};
}
