#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""临时只读监听指定 BitBrowser 窗口中的评论按钮操作。

本脚本只通过 CDP 读取页面状态并在页面内安装事件监听器，不执行点击、输入、滚动或导航。
"""

import argparse
import asyncio
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.cdp import CdpSession


INSTALL_SCRIPT = r"""
(() => {
  const key = '__codexCommentWatch';
  if (window[key]?.installed) return {installed: true, reused: true};
  const state = {installed: true, events: [], resourceCursor: 0, href: location.href};
  const push = (event) => {
    try {
      state.events.push({ts: new Date().toISOString(), href: location.href, ...event});
      if (state.events.length > 300) state.events.splice(0, state.events.length - 300);
    } catch (_) {}
  };
  const clean = (value, limit = 240) => String(value || '').replace(/\s+/g, ' ').trim().slice(0, limit);
  const describe = (node) => {
    if (!(node instanceof Element)) return null;
    const rect = node.getBoundingClientRect();
    const attrs = {};
    for (const name of ['aria-label','title','data-e2e','data-testid','data-test-id','role','id','class']) {
      if (node.getAttribute(name)) attrs[name] = clean(node.getAttribute(name), 180);
    }
    return {
      tag: node.tagName.toLowerCase(), attrs,
      text: clean(node.innerText || node.textContent),
      rect: {x: Math.round(rect.x), y: Math.round(rect.y), w: Math.round(rect.width), h: Math.round(rect.height)},
      visible: !!(rect.width || rect.height)
    };
  };
  const closest = (node) => {
    if (!(node instanceof Element)) return null;
    return describe(node.closest('button,[role="button"],a,[class*="comment" i],[data-e2e*="comment" i]'));
  };
  document.addEventListener('click', (event) => {
    push({type: 'click', x: event.clientX, y: event.clientY,
      target: describe(event.target), closest: closest(event.target)});
  }, true);
  document.addEventListener('pointerup', (event) => {
    if (event.button === 0) push({type: 'pointerup', x: event.clientX, y: event.clientY,
      target: describe(event.target), closest: closest(event.target)});
  }, true);
  const onScroll = (event) => {
    const target = event.target === document ? document.scrollingElement : event.target;
    if (!(target instanceof Element)) return;
    push({type: 'scroll', target: describe(target), top: Math.round(target.scrollTop),
      height: target.scrollHeight, viewport: target.clientHeight});
  };
  document.addEventListener('scroll', onScroll, true);
  const observer = new MutationObserver((records) => {
    const relevant = records.filter((record) => {
      const text = clean(record.target?.textContent, 300).toLowerCase();
      const attrs = clean(record.attributeName, 100).toLowerCase();
      return /评论|回复|comment|reply|note|弹窗|输入/.test(text + ' ' + attrs);
    }).slice(0, 8);
    if (!relevant.length) return;
    push({type: 'mutation', count: records.length, relevant: relevant.map((record) => ({
      kind: record.type, attribute: record.attributeName || '',
      target: describe(record.target), added: record.addedNodes?.length || 0,
      removed: record.removedNodes?.length || 0
    }))});
  });
  if (document.documentElement) observer.observe(document.documentElement,
    {subtree: true, childList: true, attributes: true, attributeFilter: ['class','aria-label','title','style','data-e2e','data-testid']});
  window[key] = state;
  push({type: 'watch_installed', title: document.title});
  return {installed: true, reused: false, href: location.href, title: document.title};
})()
"""


POLL_SCRIPT = r"""
(() => {
  const state = window.__codexCommentWatch;
  if (!state) return {missing: true, href: location.href};
  const events = state.events.splice(0, state.events.length);
  const entries = performance.getEntriesByType('resource');
  const resources = entries.slice(state.resourceCursor).map((entry) => ({
    name: entry.name, startTime: Math.round(entry.startTime),
    duration: Math.round(entry.duration), initiatorType: entry.initiatorType
  }));
  state.resourceCursor = entries.length;
  return {href: location.href, title: document.title, events, resources};
})()
"""


def utc_now():
    return datetime.now(timezone.utc).isoformat()


async def watch(ws_url: str, output: Path, seconds: float, interval: float):
    output.parent.mkdir(parents=True, exist_ok=True)
    cdp = CdpSession(ws_url, timeout=10)
    await cdp.connect()
    attached = []
    try:
        targets = await cdp.cmd('Target.getTargets')
        pages = [t for t in targets.get('targetInfos', []) if t.get('type') == 'page']
        for target in pages:
            try:
                attached_result = await cdp.cmd('Target.attachToTarget',
                    {'targetId': target['targetId'], 'flatten': True})
                sid = attached_result['sessionId']
                attached.append((sid, target))
                await cdp.cmd('Runtime.enable', session_id=sid)
                await cdp.cmd('Page.enable', session_id=sid)
                installed = await cdp.eval(INSTALL_SCRIPT, sid)
                with output.open('a', encoding='utf-8') as f:
                    f.write(json.dumps({'ts': utc_now(), 'type': 'target_attached',
                        'targetId': target.get('targetId'), 'url': target.get('url'),
                        'title': target.get('title'), 'installed': installed}, ensure_ascii=False) + '\n')
            except Exception as exc:
                with output.open('a', encoding='utf-8') as f:
                    f.write(json.dumps({'ts': utc_now(), 'type': 'target_attach_error',
                        'targetId': target.get('targetId'), 'error': repr(exc)}, ensure_ascii=False) + '\n')
        if not attached:
            raise RuntimeError('未找到可监听的 page 页面')
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            for sid, target in list(attached):
                try:
                    result = await cdp.eval(POLL_SCRIPT, sid)
                    if result.get('events') or result.get('resources'):
                        with output.open('a', encoding='utf-8') as f:
                            for event in result.get('events', []):
                                f.write(json.dumps({'ts': utc_now(), 'targetId': target.get('targetId'),
                                    'targetUrl': target.get('url'), **event}, ensure_ascii=False) + '\n')
                            for resource in result.get('resources', []):
                                f.write(json.dumps({'ts': utc_now(), 'type': 'resource',
                                    'targetId': target.get('targetId'), 'pageUrl': result.get('href'),
                                    **resource}, ensure_ascii=False) + '\n')
                        for event in result.get('events', []):
                            print(json.dumps({'target': target.get('title') or target.get('url'), **event}, ensure_ascii=False), flush=True)
                        for resource in result.get('resources', []):
                            if any(token in resource.get('name', '').lower() for token in ('comment', 'reply', 'aweme', 'note')):
                                print(json.dumps({'type': 'resource', 'target': target.get('title') or target.get('url'), **resource}, ensure_ascii=False), flush=True)
                except Exception as exc:
                    with output.open('a', encoding='utf-8') as f:
                        f.write(json.dumps({'ts': utc_now(), 'type': 'poll_error',
                            'targetId': target.get('targetId'), 'error': repr(exc)}, ensure_ascii=False) + '\n')
            await asyncio.sleep(interval)
    finally:
        await cdp.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--ws-url', required=True)
    parser.add_argument('--output', default='data/logs/comment_click_watch.jsonl')
    parser.add_argument('--seconds', type=float, default=180)
    parser.add_argument('--interval', type=float, default=0.4)
    args = parser.parse_args()
    asyncio.run(watch(args.ws_url, Path(args.output), args.seconds, args.interval))


if __name__ == '__main__':
    main()
