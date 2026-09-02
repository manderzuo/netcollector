# -*- coding: utf-8 -*-
"""通过页面可见排序控件设置平台搜索排序。"""

import json


async def click_sort_option(c, sid, labels):
    """点击页面上与候选文案完全匹配的排序控件，返回是否找到。

    页面结构经常改版，因此只依赖可见文字，不依赖平台私有 class 名。
    """
    candidates = [str(x).strip() for x in labels if str(x).strip()]
    if not candidates:
        return False
    script = """(function(labels){
      const visible = e => {
        const r=e.getBoundingClientRect();
        return r.width>0 && r.height>0 && getComputedStyle(e).visibility !== 'hidden';
      };
      const nodes=[...document.querySelectorAll('button,[role="button"],a,div,span')];
      for (const label of labels) {
        const found=nodes.find(e => visible(e) && (e.innerText||e.textContent||'').trim() === label);
        if (!found) continue;
        const target=found.closest('button,[role="button"],a') || found;
        target.click();
        return label;
      }
      return '';
    })(%s)""" % json.dumps(candidates, ensure_ascii=False)
    try:
        return str(await c.eval(script, sid) or "")
    except Exception:
        return ""
