export function el(tag, attrs={}, ...children) {
  const node=document.createElement(tag);
  for (const [key,value] of Object.entries(attrs)) {
    if (key.startsWith('on')) node.addEventListener(key.slice(2).toLowerCase(),value);
    else if (key === 'class') node.className=value;
    else if (value !== null && value !== undefined && value !== false) node.setAttribute(key,String(value));
  }
  node.append(...children.flat().filter(c=>c !== null && c !== undefined).map(c=>c instanceof Node ? c : document.createTextNode(String(c))));
  return node;
}

export function skeleton(label='Loading…') {
  return el('div',{class:'loading-skeleton',role:'status','aria-live':'polite'},
    el('span',{class:'small muted'},label),
    el('div',{'aria-hidden':'true',class:'skeleton-line skeleton-title'}),
    ...Array.from({length:4},()=>el('div',{'aria-hidden':'true',class:'skeleton-line'})));
}
