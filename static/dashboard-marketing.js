(function () {
  function money(n) {
    return Number(n || 0).toLocaleString('ru-RU');
  }

  function text(value) {
    return String(value || '').replace(/[&<>"']/g, ch => ({
      '&': '&amp;',
      '<': '&lt;',
      '>': '&gt;',
      '"': '&quot;',
      "'": '&#039;',
    }[ch]));
  }

  function tr(key, vars) {
    let value = (typeof window.t === 'function') ? window.t(key) : key;
    if (value === key) return key;
    Object.entries(vars || {}).forEach(([name, replacement]) => {
      value = value.replace(`{${name}}`, replacement);
    });
    return value;
  }

  function setReply(message, kind) {
    const el = document.getElementById('marketingAssistantReply');
    if (!el) return;
    el.textContent = message || '';
    el.classList.toggle('warn', kind === 'warn');
  }

  function payload() {
    const platforms = (document.getElementById('marketingPlatforms')?.value || '')
      .split(',')
      .map(v => v.trim().toLowerCase())
      .filter(Boolean);
    return {
      site_id: document.getElementById('marketingSiteId')?.value || '',
      goal: document.getElementById('marketingGoal')?.value || '',
      target_audience: document.getElementById('marketingAudience')?.value || '',
      location: document.getElementById('marketingLocation')?.value || '',
      budget: Number(document.getElementById('marketingBudget')?.value || 0),
      budget_credits: Number(document.getElementById('marketingBudget')?.value || 0),
      platforms,
      platform: platforms[0] || 'instagram',
      objective: document.getElementById('marketingObjective')?.value || '',
      content_type: 'campaign_pack',
      status: 'active',
      auto_optimize: true,
    };
  }

  function renderCounters() {
    document.querySelectorAll('[data-marketing-counters] [data-count]').forEach(el => {
      const target = Number(el.dataset.count || 0);
      const suffix = el.dataset.suffix || '';
      const start = performance.now();
      const duration = 700;
      function tick(now) {
        const t = Math.min(1, (now - start) / duration);
        const value = target * (1 - Math.pow(1 - t, 3));
        el.textContent = `${money(suffix ? value.toFixed(1) : Math.round(value))}${suffix}`;
        if (t < 1) requestAnimationFrame(tick);
      }
      requestAnimationFrame(tick);
    });
  }

  function renderChart() {
    const el = document.getElementById('marketingTrendChart');
    const src = document.getElementById('marketingTimelineData');
    if (!el || !src) return;
    let rows = [];
    try { rows = JSON.parse(src.textContent || '[]') } catch (e) { rows = [] }
    if (!rows.length) {
      el.innerHTML = `<div class="marketing-chart-empty" data-i18n="marketing_chart_empty">${text(tr('marketing_chart_empty'))}</div>`;
      return;
    }
    const max = Math.max(...rows.map(r => Number(r.visitors || 0) + Number(r.clicks || 0)), 1);
    el.innerHTML = rows.map(r => {
      const total = Number(r.visitors || 0) + Number(r.clicks || 0);
      const h = Math.max(8, Math.round((total / max) * 150));
      return `<div class="marketing-bar ${total ? '' : 'empty'}" style="height:${h}px" title="${text(r.date)}: ${total}"></div>`;
    }).join('');
  }

  function appendContent(result) {
    const list = document.getElementById('marketingContentList');
    if (!list || !result || !result.content_id) return;
    const summary = result.content?.summary || tr('marketing_content_ready_summary');
    const html = `<article class="marketing-row marketing-content-row">
      <div>
        <strong>campaign_pack · new</strong>
        <span>${money(result.credits_spent)} ${text(tr('bill_credits_word'))}</span>
        <p>${text(summary)}</p>
      </div>
      <div class="marketing-row-actions">
        <button class="soft" onclick="marketingSaveContent(${Number(result.content_id)})" data-i18n="marketing_save">${text(tr('marketing_save'))}</button>
        <button class="soft" onclick="marketingRegenerateContent(${Number(result.content_id)})" data-i18n="marketing_regenerate">${text(tr('marketing_regenerate'))}</button>
      </div>
    </article>`;
    if (list.querySelector('.marketing-empty')) list.innerHTML = html;
    else list.insertAdjacentHTML('afterbegin', html);
  }

  window.marketingScrollToAssistant = function () {
    document.getElementById('marketingAssistant')?.scrollIntoView({ behavior: 'smooth', block: 'start' });
  };

  window.marketingAssistantCheck = async function () {
    try {
      const data = await postJson('/api/marketing/assistant/message', payload());
      setReply(data.reply || tr('marketing_default_ready'));
    } catch (e) {
      setReply(e.message, 'warn');
    }
  };

  window.marketingGenerateContent = async function () {
    setReply(tr('marketing_generating'));
    try {
      const data = await postJson('/api/marketing/content/generate', payload());
      if (data.ready === false) {
        setReply(data.reply || tr('marketing_need_more'), 'warn');
        return;
      }
      appendContent(data);
      setReply(tr('marketing_generated_reply', { val: data.credits_spent || 0 }));
      toast(tr('marketing_generated_toast'));
    } catch (e) {
      setReply(e.message, 'warn');
    }
  };

  window.marketingLaunchCampaign = async function () {
    try {
      const data = await postJson('/api/marketing/campaigns', payload());
      toast(tr('marketing_campaign_started'));
      setTimeout(() => location.reload(), 700);
      return data;
    } catch (e) {
      setReply(e.message, 'warn');
    }
  };

  window.marketingCampaignAction = async function (id, action) {
    try {
      const data = await postJson(`/api/marketing/campaigns/${Number(id)}/${action}`);
      if (action === 'improve' && data.suggestions) {
        setReply((data.suggestions.actions || []).join(' '));
        toast(tr('marketing_improvements_ready'));
        return;
      }
      toast(tr('marketing_campaign_updated'));
      setTimeout(() => location.reload(), 500);
    } catch (e) {
      toast(e.message);
    }
  };

  window.marketingSaveContent = async function (id) {
    try {
      await postJson(`/api/marketing/content/${Number(id)}/save`);
      toast(tr('marketing_draft_saved'));
    } catch (e) {
      toast(e.message);
    }
  };

  window.marketingRegenerateContent = async function (id) {
    setReply(tr('marketing_regenerating'));
    try {
      const data = await postJson(`/api/marketing/content/${Number(id)}/regenerate`);
      appendContent(data);
      setReply(tr('marketing_regenerated_reply', { val: data.credits_spent || 0 }));
    } catch (e) {
      setReply(e.message, 'warn');
    }
  };

  renderCounters();
  renderChart();
})();
