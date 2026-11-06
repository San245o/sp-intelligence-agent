/* Reuse the existing dashboard renderers; compute every summary from accepted data. */
(() => {
  const signals = x => x.external?.signals || [];
  const news = x => signals(x).filter(s => ['public_post','public_mention'].includes(s.signal));
  const date = s => s.publishedAt || s.metrics?.activity_date || s.metrics?.published_at || '';
  const website = x => x.web?.status === 'available' && x.web?.value?.identity_assessment?.publishable === true && x.web?.value?.identity_assessment?.content_attributable === true;
  const total = predicate => DATA.reduce((n,x) => n + signals(x).filter(predicate).length,0);
  const companySocial = DATA.filter(x => signals(x).some(s => s.signal === 'profile_handle')).length;
  const stats = [
    [DATA.length,'companies in this snapshot'],
    [DATA.filter(x => x.financial?.status === 'available').length,'annual accounts returned'],
    [DATA.filter(website).length,'verified website URLs'],
    [companySocial,'companies with verified social links'],
    [total(s => ['public_post','public_mention','job_posting'].includes(s.signal)),'dated activity and job records']
  ];
  document.querySelector('.stats').innerHTML = stats.map(([n,label])=>`<div class="stat"><strong>${n}</strong><span>${esc(label)}</span></div>`).join('');
  const snapshot = document.querySelector('.snapshot');
  if(snapshot) snapshot.textContent = `${DATA.length} companies · ${companySocial} with verified social links`;
  const hero = document.querySelector('.hero h1');if(hero)hero.textContent='Research a company. Inspect the evidence.';
  const lede=document.querySelector('.lede');if(lede)lede.textContent='Find a company, review its official records, and inspect verified websites, news, reviews and hiring. Missing evidence is shown explicitly.';
  const sidebar=document.querySelector('.controls');
  const tools=document.createElement('div');tools.className='review-tools';tools.innerHTML=`<select id="evidence-filter" aria-label="Evidence category"><option value="">All evidence coverage</option><option value="website">Verified website</option><option value="missing-website">Website unresolved</option><option value="news">Dated news</option><option value="reviews">Ratings or reviews</option><option value="jobs">Job postings</option><option value="workforce">Workforce evidence</option><option value="engagement">Audience metrics</option></select><select id="evidence-sort" aria-label="Company sort order"><option value="name">Company name</option><option value="evidence">Most verified evidence</option><option value="recent">Most recent publication</option></select>`;sidebar.insertBefore(tools,sidebar.querySelector('.workspace-actions'));
  const baseFiltered=filtered;
  filtered=()=>{
    const kind=document.querySelector('#evidence-filter').value;
    const types={news:['public_post','public_mention'],reviews:['review','review_summary','place_summary'],jobs:['job_posting'],workforce:['workforce_snapshot'],engagement:['profile_metrics','buzz_metrics']}[kind];
    const rows=baseFiltered().filter(x=>!kind || (kind==='website'?website(x):kind==='missing-website'?!website(x):signals(x).some(s=>types.includes(s.signal))));
    const sort=document.querySelector('#evidence-sort').value;
    if(sort==='evidence')rows.sort((a,b)=>signals(b).length-signals(a).length);
    if(sort==='recent')rows.sort((a,b)=>(news(b).map(date).sort().pop()||'').localeCompare(news(a).map(date).sort().pop()||''));
    return rows;
  };
  tools.querySelectorAll('select').forEach(el=>el.addEventListener('change',()=>{selected=null;renderList();renderProfile();}));
  const baseExternal=renderExternalLayer;
  renderExternalLayer=x=>{
    const holder=document.createElement('div');holder.innerHTML=baseExternal(x);
    holder.querySelector('.activity-list').innerHTML=signals(x).filter(s=>s.signal!=='profile_handle').sort((a,b)=>date(b).localeCompare(date(a))).slice(0,40).map(s=>{
      const company=(s.sourceClass||'').startsWith('company_')||s.platform==='company_site';
      const label=['public_post','public_mention'].includes(s.signal)?company?'Company-published':'Independent publisher':s.signal.replaceAll('_',' ');
      const when=date(s);
      const metrics=Object.entries(s.metrics||{}).filter(([k])=>['rating','review_count','employees','full_time_equivalents','views','likes','comments','followers','job_title','deadline'].includes(k)).map(([k,v])=>`${esc(k.replaceAll('_',' '))}: ${esc(v)}`).join(' · ');
      return `<div class="activity"><span class="publication-label">${esc(label)}</span><small>${when?`Published ${esc(when.slice(0,10))}`:'Publication date not captured'}</small><p>${esc((s.text||s.metrics?.title||'Captured evidence').slice(0,500))}</p>${metrics?`<p>${metrics}</p>`:''}<small>Retrieved ${esc(s.retrievedAt||'not captured')}${cite(s.source)}</small>${sourceLine(s)}</div>`;
    }).join('')||'<p class="empty">No verified external observations captured. This does not establish absence.</p>';
    return holder.innerHTML;
  };
  const baseRequested=requestedModes;
  requestedModes=question=>{const modes=baseRequested(question);if(/publication|published|article|news|nyhet/i.test(question))modes.push('activity');return [...new Set(modes)];};
  const baseAgent=agentBody;
  agentBody=(x,mode)=>{
    if(mode==='activity'){
      const items=news(x).sort((a,b)=>date(b).localeCompare(date(a)));
      if(!items.length)return '<p>No verified dated news captured for this company.</p>';
      const latest=date(items[0]);
      return `<p><strong>${items.length} captured news item(s).</strong> ${latest?`Latest supported publication date: <strong>${esc(latest.slice(0,10))}</strong>.`:'No supported publication date is recorded.'}</p>`+items.slice(0,5).map(s=>`<p>${date(s)?`<small>${esc(date(s).slice(0,10))}</small><br>`:''}${esc(s.text||s.metrics?.title||'Captured publication')}${cite(s.source)}</p>`).join('');
    }
    if(mode==='social'){
      const handles=(x.external?.handles||[]);
      if(handles.length)return handles.map(s=>`<p><strong>${esc(s.platform)}</strong>${cite(s.url)}</p>`).join('');
    }
    return baseAgent(x,mode);
  };
  const baseProfile=renderProfile;
  renderProfile=()=>{
    baseProfile();
    const x=selected;if(!x)return;
    const proof=x.web?.value?.identity_assessment||{};
    if(x.web?.status==='available' && (!proof.publishable||!proof.content_attributable)){
      const chip=[...document.querySelectorAll('#profile .chip')].find(el=>el.textContent.startsWith('website:'));
      if(chip){chip.textContent=proof.publishable?'website: associated URL':'website: unverified';chip.classList.remove('good');chip.classList.add('warn');}
      const label=[...document.querySelectorAll('#profile .section-label')].find(el=>el.textContent==='Company-controlled web layer');
      if(label){label.textContent=proof.publishable?'Registry-associated URL':'Website candidate';const paragraph=label.parentElement.querySelector('p');if(paragraph)paragraph.textContent='Captured content has not been attributed to this exact legal entity. Open the evidence to inspect the source.';}
    }
  };
  renderList();renderProfile();renderAgent();
})();
