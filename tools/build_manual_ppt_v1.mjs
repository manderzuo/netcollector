import fs from "node:fs/promises";
import path from "node:path";
import { pathToFileURL } from "node:url";
import { Presentation, PresentationFile } from "@oai/artifact-tool";

const { SKILL_DIR, TMP_DIR } = process.env;
if (!path.isAbsolute(SKILL_DIR ?? "") || !path.isAbsolute(TMP_DIR ?? "")) {
  throw new Error("Set absolute SKILL_DIR and TMP_DIR");
}

const { resolvePresentationFont } = await import(
  pathToFileURL(path.join(SKILL_DIR, "container_tools/artifact_tool_utils.mjs")).href,
);

const W = 1280;
const H = 720;
const C = {
  bg: "#081423",
  panel: "#10243A",
  panel2: "#162D48",
  panel3: "#1C3B5D",
  ink: "#F4F7FB",
  muted: "#A7B8CC",
  blue: "#70A9FF",
  cyan: "#4DD7E8",
  green: "#35D4A0",
  amber: "#F4C15D",
  red: "#FF7185",
  line: "#2A4A6B",
  white: "#FFFFFF",
};
const family = resolvePresentationFont();
const root = "D:\\gpt\\douyinxiaohongshu";
const asset = (p) => path.join(root, p);
const temp = (p) => path.join("C:\\Users\\StarLink\\AppData\\Local\\Temp", p);
const files = {
  logo: asset("assets\\brand_logo.png"),
  icon: asset("assets\\user_app_icon.png"),
  overview: asset("ui-current-overview.png"),
  tasks: asset("ui-current-task-center.png"),
  accounts: asset("ui-current-account-management.png"),
  leads: temp("codex-clipboard-656fbac2-424b-4f73-b8c1-8e20ff98eacd.png"),
  newTask: temp("codex-clipboard-410ea212-fd12-4b9a-b238-fd0af112a3fd.png"),
  publish: temp("codex-clipboard-caf2c194-d854-4b74-986d-e40edcf99cfc.png"),
  xhs: asset("assets\\platform_xhs.png"),
  douyin: asset("assets\\platform_douyin.png"),
  bilibili: asset("assets\\platform_bilibili.png"),
  weibo: asset("assets\\platform_weibo.webp"),
};

async function exists(p) {
  try { await fs.access(p); return true; } catch { return false; }
}
async function imageBytes(p) {
  return new Uint8Array(await fs.readFile(p));
}
function contentType(p) {
  const ext = path.extname(p).toLowerCase();
  return ext === ".jpg" || ext === ".jpeg" ? "image/jpeg" : ext === ".webp" ? "image/webp" : "image/png";
}
function addBox(slide, x, y, w, h, fill, radius = 18, line = C.line) {
  return slide.shapes.add({
    geometry: "roundRect",
    position: { left: x, top: y, width: w, height: h },
    fill,
    line: { style: "solid", fill: line, width: 1 },
    borderRadius: radius,
  });
}
function addRect(slide, x, y, w, h, fill, line = { style: "solid", fill: fill, width: 0 }) {
  return slide.shapes.add({ geometry: "rect", position: { left: x, top: y, width: w, height: h }, fill, line });
}
function addText(slide, text, x, y, w, h, size = 22, color = C.ink, bold = false, extra = {}) {
  const shape = slide.shapes.add({
    geometry: "textbox",
    position: { left: x, top: y, width: w, height: h },
    fill: "none",
    line: { style: "solid", fill: "none", width: 0 },
  });
  shape.text = text;
  shape.text.style = { typeface: family, fontSize: size, color, bold, autoFit: "none", ...extra };
  return shape;
}
function addRule(slide, x, y, w, color = C.line, thickness = 1) {
  slide.shapes.add({ geometry: "line", position: { left: x, top: y, width: w, height: 0 }, fill: "none", line: { style: "solid", fill: color, width: thickness } });
}
async function addImage(slide, p, x, y, w, h, alt, fit = "contain", crop) {
  if (!(await exists(p))) return false;
  slide.images.add({
    blob: await imageBytes(p),
    contentType: contentType(p),
    alt,
    fit,
    position: { left: x, top: y, width: w, height: h },
    geometry: "roundRect",
    borderRadius: "rounded-xl",
    ...(crop ? { crop } : {}),
  });
  return true;
}
function footer(slide, n, label = "多平台采集工作台 · 产品与操作说明") {
  addRule(slide, 72, 681, 1136, C.line, 1);
  addText(slide, label, 72, 692, 760, 18, 11, C.muted, false);
  addText(slide, String(n).padStart(2, "0"), 1148, 690, 60, 20, 12, C.muted, true, { align: "right" });
}
function title(slide, kicker, main, sub = "") {
  addText(slide, kicker.toUpperCase(), 72, 38, 600, 22, 12, C.cyan, true);
  addText(slide, main, 72, 67, 1080, 48, 34, C.ink, true);
  if (sub) addText(slide, sub, 72, 119, 1080, 28, 15, C.muted, false);
}
function pill(slide, text, x, y, w, color = C.blue) {
  addBox(slide, x, y, w, 30, color, 15, color);
  addText(slide, text, x + 12, y + 6, w - 24, 18, 12, C.bg, true);
}
function bullet(slide, text, x, y, w, accent = C.blue, size = 18) {
  addBox(slide, x, y + 4, 9, 9, accent, 5, accent);
  addText(slide, text, x + 22, y, w - 22, 30, size, C.ink, false);
}
function metric(slide, x, y, w, label, value, color = C.blue) {
  addBox(slide, x, y, w, 116, C.panel, 16, C.line);
  addText(slide, label, x + 20, y + 18, w - 40, 22, 13, C.muted, false);
  addText(slide, value, x + 20, y + 47, w - 40, 42, 30, color, true);
}
function screenshotLabel(slide, text, x, y, w, color = C.cyan) {
  addText(slide, text, x, y, w, 18, 11, color, true);
}
function step(slide, no, heading, body, x, y, color = C.blue) {
  addBox(slide, x, y, 62, 62, color, 31, color);
  addText(slide, String(no), x + 18, y + 15, 28, 30, 22, C.bg, true);
  addText(slide, heading, x + 84, y + 3, 340, 28, 19, C.ink, true);
  addText(slide, body, x + 84, y + 34, 370, 44, 14, C.muted, false);
}

const presentation = Presentation.create({ slideSize: { width: W, height: H } });
function base() {
  const slide = presentation.slides.add();
  slide.background.fill = C.bg;
  return slide;
}

// 1 cover
{
  const s = base();
  addRect(s, 0, 0, W, 12, C.cyan);
  await addImage(s, files.logo, 72, 82, 92, 92, "工作台品牌图标", "contain");
  addText(s, "多平台采集工作台", 72, 208, 760, 66, 46, C.ink, true);
  addText(s, "产品与操作说明 · 第一版", 76, 286, 660, 34, 24, C.blue, true);
  addText(s, "采集 · 线索 · 互动 · 发布 · 消息 · 设置", 76, 348, 720, 30, 17, C.muted, false);
  addRule(s, 76, 414, 470, C.line, 2);
  addText(s, "面向团队协作的统一工作台\n覆盖抖音、小红书、B站、微博及快手等平台", 76, 438, 570, 64, 16, C.ink, false);
  await addImage(s, files.overview, 775, 84, 430, 500, "采集总览真实界面截图", "cover", { left: 0.04, top: 0.04, right: 0.02, bottom: 0.03 });
  screenshotLabel(s, "真实界面截图 · 当前版本", 795, 604, 340, C.cyan);
  footer(s, 1);
}

// 2 positioning
{
  const s = base(); title(s, "01 · 认识工作台", "把分散的平台操作，收拢成一条可追踪链路", "从发现内容到完成互动，每一步都有状态、日志和数据落点。");
  metric(s, 72, 192, 246, "覆盖平台", "5 个", C.cyan);
  metric(s, 338, 192, 246, "核心工作区", "采集 + 发布", C.blue);
  metric(s, 604, 192, 246, "协作结果", "线索 / 互动", C.green);
  metric(s, 870, 192, 246, "风险控制", "人工接管", C.amber);
  addBox(s, 72, 354, 1044, 232, C.panel, 18, C.line);
  addText(s, "系统定位", 102, 384, 200, 24, 17, C.cyan, true);
  bullet(s, "统一管理账号、关键词组、采集任务和发布内容", 104, 430, 910, C.blue);
  bullet(s, "将评论转化为可筛选、可审核、可互动的线索", 104, 475, 910, C.green);
  bullet(s, "保留模拟模式，真实回复 / 真实发布由人工开关控制", 104, 520, 910, C.amber);
  footer(s, 2);
}

// 3 workflow
{
  const s = base(); title(s, "02 · 全流程", "一条线看懂：从搜索到可回访", "建议按顺序使用，遇到登录、验证码或网络问题时随时暂停并人工接管。");
  const xs = [100, 330, 560, 790, 1020];
  const heads = ["准备账号", "搜索采集", "筛选线索", "审核互动", "复盘跟进"];
  const bodies = ["绑定窗口\n确认平台状态", "关键词组\n逐词采集作品与评论", "任务 / 平台 / 时间\n意向并行筛选", "评论回复 / 私信\n模拟或真实发送", "消息中心\n导出与数据核对"];
  for (let i = 0; i < xs.length; i++) {
    addBox(s, xs[i], 250, 150, 128, i === 2 ? C.panel3 : C.panel, 18, i === 2 ? C.cyan : C.line);
    addText(s, String(i + 1).padStart(2, "0"), xs[i] + 18, 268, 52, 26, 15, C.cyan, true);
    addText(s, heads[i], xs[i] + 18, 304, 120, 24, 18, C.ink, true);
    addText(s, bodies[i], xs[i] + 18, 338, 120, 40, 13, C.muted, false);
    if (i < xs.length - 1) addText(s, "→", xs[i] + 168, 292, 58, 40, 28, C.blue, true, { align: "center" });
  }
  addText(s, "暂停 / 继续 / 人工验证 / 日志记录", 360, 466, 560, 28, 17, C.amber, true, { align: "center" });
  addRule(s, 365, 510, 550, C.amber, 2);
  addText(s, "任何一个节点都可以回到任务状态中继续处理，避免重复采集和数据丢失。", 220, 546, 840, 28, 16, C.muted, false, { align: "center" });
  footer(s, 3);
}

// 4 auth
{
  const s = base(); title(s, "03 · 账号与权限", "先登录，再协作；数据按员工隔离", "管理员审批注册申请，员工只看到自己的账号、任务和线索。");
  step(s, 1, "登录 / 注册", "员工填写账号、六位以上密码和姓名。", 92, 214, C.blue);
  step(s, 2, "管理员审批", "申请进入管理员中心，审批通过后方可使用。", 92, 324, C.cyan);
  step(s, 3, "个人中心", "查看姓名、上传数据、退出登录和审批入口。", 92, 434, C.green);
  addBox(s, 680, 198, 430, 336, C.panel, 18, C.line);
  addText(s, "数据隔离原则", 714, 230, 320, 28, 20, C.ink, true);
  bullet(s, "员工 A 只能看到自己的数据", 718, 292, 350, C.blue);
  bullet(s, "员工 B 不会覆盖或干扰员工 A", 718, 344, 350, C.cyan);
  bullet(s, "管理员可查看全部员工数据", 718, 396, 350, C.green);
  bullet(s, "同步接口可保持为空，后续再配置", 718, 448, 350, C.amber);
  footer(s, 4);
}

// 5 account management actual
{
  const s = base(); title(s, "04 · 账号信息", "一个平台可以绑定多个浏览器账号", "先在比特浏览器创建并打开窗口，再在工作台中同步、识别昵称与平台状态。");
  addBox(s, 72, 182, 720, 428, C.panel, 18, C.line);
  await addImage(s, files.accounts, 86, 196, 692, 394, "账号管理真实界面截图", "contain");
  screenshotLabel(s, "真实界面截图 · 账号管理", 94, 590, 340, C.cyan);
  addBox(s, 844, 196, 330, 336, C.panel2, 18, C.line);
  addText(s, "操作要点", 874, 228, 230, 26, 20, C.ink, true);
  bullet(s, "创建窗口", 878, 282, 250, C.blue);
  bullet(s, "绑定窗口 ID", 878, 330, 250, C.cyan);
  bullet(s, "读取昵称 / 平台", 878, 378, 250, C.green);
  bullet(s, "打开浏览器测试", 878, 426, 250, C.amber);
  bullet(s, "异常时释放账号", 878, 474, 250, C.red);
  footer(s, 5);
}

// 6 keyword groups
{
  const s = base(); title(s, "05 · 关键词组", "把核心词保存成可复用的搜索方案", "组内关键词逐个执行；每个关键词独立达到目标数量或明确提示无更多内容后再进入下一个。");
  addBox(s, 72, 194, 476, 366, C.panel, 18, C.line);
  addText(s, "关键词组示例", 104, 226, 300, 26, 20, C.ink, true);
  pill(s, "河南本地生活", 104, 276, 146, C.cyan);
  addText(s, "核心词：互联网洗衣、干洗店、洗鞋店", 104, 332, 360, 30, 16, C.ink, false);
  addText(s, "适用平台：抖音 · 小红书 · B站 · 微博 · 快手", 104, 386, 370, 30, 14, C.muted, false);
  addRule(s, 104, 442, 360, C.line, 1);
  addText(s, "新建任务时选择该组，即可自动逐词搜索。", 104, 468, 360, 50, 15, C.muted, false);
  addBox(s, 598, 194, 518, 366, C.panel2, 18, C.line);
  addText(s, "执行规则", 630, 226, 300, 26, 20, C.ink, true);
  bullet(s, "每个关键词按目标数量独立计数", 632, 284, 420, C.blue);
  bullet(s, "中断后恢复当前关键词，不跳词", 632, 338, 420, C.cyan);
  bullet(s, "滚动加载、等待新内容、URL 去重", 632, 392, 420, C.green);
  bullet(s, "无更多提示写入日志并结束该词", 632, 446, 420, C.amber);
  footer(s, 6);
}

// 7 new task actual/reference
{
  const s = base(); title(s, "06 · 新建采集任务", "用一张表把采集边界说清楚", "平台、关键词组、排序、目标数量、账号分配和输出目录都在任务创建时确定。");
  addBox(s, 72, 180, 668, 438, C.panel, 18, C.line);
  await addImage(s, files.newTask, 88, 196, 636, 406, "新建任务界面参考截图", "contain");
  screenshotLabel(s, "参考界面 · 需以实际运行版本为准", 96, 590, 380, C.amber);
  addBox(s, 792, 202, 356, 310, C.panel2, 18, C.line);
  addText(s, "创建前检查", 824, 232, 280, 26, 20, C.ink, true);
  bullet(s, "目标数量 = 每个关键词的目标", 826, 286, 290, C.blue);
  bullet(s, "排序规则按平台分别选择", 826, 336, 290, C.cyan);
  bullet(s, "采集全部可加载评论", 826, 386, 290, C.green);
  bullet(s, "输出目录可自由选择", 826, 436, 290, C.amber);
  footer(s, 7);
}

// 8 collection rules
{
  const s = base(); title(s, "07 · 采集过程", "滚动、等待、去重，直到真正到达尽头", "平台页面加载节奏不同，但任务判断遵循同一套恢复逻辑。");
  addBox(s, 72, 206, 522, 342, C.panel, 18, C.line);
  addText(s, "采集循环", 104, 238, 260, 26, 20, C.ink, true);
  const lines = ["读取当前可见作品", "进入详情并打开评论区", "滚动加载下一批评论", "10 秒内有新内容就继续", "重复内容只去重，不判定结束", "看到“暂时没有更多了”才结束"];
  lines.forEach((t, i) => bullet(s, t, 108, 286 + i * 40, 420, i === 5 ? C.amber : C.blue, 15));
  addBox(s, 650, 206, 468, 342, C.panel2, 18, C.line);
  addText(s, "中断后的恢复", 682, 238, 300, 26, 20, C.ink, true);
  bullet(s, "任务状态变为：暂停 / 待人工验证", 686, 294, 390, C.amber);
  bullet(s, "人工处理完成后点击“继续”", 686, 344, 390, C.cyan);
  bullet(s, "回到当前关键词和当前阶段", 686, 394, 390, C.green);
  bullet(s, "搜索未完成就继续搜索，不直接采评论", 686, 444, 390, C.red);
  footer(s, 8);
}

// 9 overview actual
{
  const s = base(); title(s, "08 · 采集总览", "先看全局，再进入具体任务", "概览页只呈现变化中的指标；任务列队、评论数、状态和平台分布一眼可见。");
  await addImage(s, files.overview, 72, 186, 1136, 430, "采集总览真实界面截图", "contain");
  screenshotLabel(s, "真实界面截图 · 采集总览", 84, 626, 350, C.cyan);
  footer(s, 9);
}

// 10 task center actual
{
  const s = base(); title(s, "09 · 任务中心", "状态是下一步动作的提示", "暂停显示“开始”，工作中显示“暂停”，人工验证显示“继续”；异常原因要跟随任务保存。");
  addBox(s, 72, 184, 780, 426, C.panel, 18, C.line);
  await addImage(s, files.tasks, 88, 200, 748, 390, "任务中心真实界面截图", "contain");
  screenshotLabel(s, "真实界面截图 · 任务中心", 96, 592, 320, C.cyan);
  addBox(s, 894, 202, 292, 322, C.panel2, 18, C.line);
  addText(s, "常见状态", 924, 232, 220, 26, 20, C.ink, true);
  pill(s, "待采集", 924, 280, 92, C.blue);
  pill(s, "采集中", 1028, 280, 92, C.cyan);
  pill(s, "暂停", 924, 328, 92, C.amber);
  pill(s, "待人工验证", 1028, 328, 126, C.red);
  pill(s, "暂无更多", 924, 376, 108, C.green);
  pill(s, "已完成", 1044, 376, 92, C.green);
  addText(s, "任务编号、平台、账号、进度、评论数、状态\n固定列宽对齐，避免切换和刷新时跳动。", 924, 438, 236, 60, 14, C.muted, false);
  footer(s, 10);
}

// 11 leads actual
{
  const s = base(); title(s, "10 · 线索中心", "把评论变成可处理的线索", "任务、平台、地区、时间和意向并行筛选；原作地址支持点击回到来源内容。");
  addBox(s, 72, 174, 880, 444, C.panel, 18, C.line);
  await addImage(s, files.leads, 86, 188, 852, 414, "线索中心真实界面截图", "contain");
  screenshotLabel(s, "真实界面截图 · 线索中心", 96, 590, 320, C.cyan);
  addBox(s, 988, 200, 202, 318, C.panel2, 18, C.line);
  addText(s, "操作路径", 1012, 232, 150, 26, 19, C.ink, true);
  bullet(s, "选择任务", 1014, 286, 154, C.blue, 14);
  bullet(s, "筛选意向", 1014, 332, 154, C.cyan, 14);
  bullet(s, "全选 / 导出", 1014, 378, 154, C.green, 14);
  bullet(s, "加入互动中心", 1014, 424, 154, C.amber, 14);
  footer(s, 11);
}

// 12 interaction
{
  const s = base(); title(s, "11 · 互动中心", "先审核，再决定评论回复还是私信", "进入互动中心即代表人工筛选通过；模拟模式只填入不发送，真实发送由开关控制。");
  addBox(s, 72, 196, 336, 320, C.panel, 18, C.line);
  addText(s, "待生成", 104, 232, 110, 26, 20, C.ink, true);
  addText(s, "选择：进入发送页 / 删除", 104, 286, 220, 24, 15, C.muted, false);
  addText(s, "原评论全文", 104, 342, 220, 24, 15, C.cyan, true);
  addText(s, "支持从线索中心后台加入，页面不跳转。", 104, 394, 240, 48, 14, C.muted, false);
  addBox(s, 470, 196, 336, 320, C.panel2, 18, C.line);
  addText(s, "待发送", 502, 232, 110, 26, 20, C.ink, true);
  addText(s, "选择账号 / 发送 / 删除", 502, 286, 220, 24, 15, C.muted, false);
  addText(s, "评论回复 ↔ 私信", 502, 342, 220, 24, 15, C.cyan, true);
  addText(s, "连续互动默认冷却 5 秒，连续 10 条冷却 1 分钟。", 502, 394, 250, 48, 14, C.muted, false);
  addBox(s, 868, 196, 304, 320, C.panel3, 18, C.cyan);
  addText(s, "已回复 / 失败", 900, 232, 220, 26, 20, C.ink, true);
  addText(s, "记录平台、账号、任务、回复话术\n以及失败原因，便于回访和复盘。", 900, 286, 220, 64, 15, C.ink, false);
  pill(s, "人工确认", 900, 408, 100, C.green);
  pill(s, "日志留痕", 1012, 408, 100, C.blue);
  footer(s, 12);
}

// 13 templates
{
  const s = base(); title(s, "12 · 回复模板", "模板是完整话术，不是摘要标签", "可新建、自定义变量、选择后载入输入框，再按当前评论微调。");
  addBox(s, 72, 202, 464, 334, C.panel, 18, C.line);
  addText(s, "模板管理", 104, 234, 260, 26, 20, C.ink, true);
  pill(s, "新增模板", 104, 284, 108, C.blue);
  addText(s, "模板名称：本地服务咨询", 104, 344, 340, 24, 16, C.ink, false);
  addText(s, "可用变量：用户昵称、平台、评论原文、作品标题", 104, 390, 370, 24, 14, C.muted, false);
  addText(s, "变量名称使用中文，保存后可重复调用。", 104, 444, 330, 24, 14, C.cyan, false);
  addBox(s, 594, 202, 614, 334, C.panel2, 18, C.line);
  addText(s, "完整回复内容", 626, 234, 260, 26, 20, C.ink, true);
  addBox(s, 626, 286, 540, 174, C.bg, 12, C.line);
  addText(s, "您好，【用户昵称】！看到您对【评论原文】感兴趣，\n如果需要了解具体方案，可以告诉我您的地区和需求，\n我会为您整理合适的服务信息。", 650, 316, 492, 108, 16, C.ink, false);
  addText(s, "选择模板 → 自动填充 → 人工修改 → 审核发送", 626, 488, 500, 24, 15, C.green, true);
  footer(s, 13);
}

// 14 publish actual
{
  const s = base(); title(s, "13 · 发布中心", "内容生成、审核、定时发布一体化", "发布前按平台校验文案与素材类型；真实发布默认关闭，定时发布时间以北京时间为准。");
  addBox(s, 72, 176, 794, 440, C.panel, 18, C.line);
  await addImage(s, files.publish, 88, 190, 762, 408, "发布中心真实界面截图", "contain");
  screenshotLabel(s, "真实界面截图 · 发布中心", 96, 592, 320, C.cyan);
  addBox(s, 906, 202, 282, 328, C.panel2, 18, C.line);
  addText(s, "发布检查", 936, 234, 200, 26, 20, C.ink, true);
  bullet(s, "账号与平台匹配", 938, 286, 220, C.blue, 14);
  bullet(s, "图片 / 视频模式匹配", 938, 332, 220, C.cyan, 14);
  bullet(s, "文案与本地版本比对", 938, 378, 220, C.green, 14);
  bullet(s, "检测冷却后再点击发布", 938, 424, 220, C.amber, 14);
  bullet(s, "失败原因写入日志", 938, 470, 220, C.red, 14);
  footer(s, 14);
}

// 15 message center
{
  const s = base(); title(s, "14 · 消息中心", "把发布后的互动带回工作台", "按平台同步可提取的消息；同一人的消息归类为一个对话，点击后查看右侧详情。");
  addBox(s, 72, 194, 370, 350, C.panel, 18, C.line);
  addText(s, "对话列表", 104, 228, 220, 26, 20, C.ink, true);
  const names = ["用户 A  ·  抖音", "用户 B  ·  小红书", "用户 C  ·  B站", "用户 D  ·  微博"];
  names.forEach((n, i) => {
    addBox(s, 104, 282 + i * 56, 300, 42, i === 0 ? C.panel3 : C.panel2, 10, i === 0 ? C.cyan : C.line);
    addText(s, n, 122, 293 + i * 56, 250, 22, 15, C.ink, i === 0);
  });
  addBox(s, 480, 194, 700, 350, C.panel2, 18, C.line);
  addText(s, "对话详情", 512, 228, 220, 26, 20, C.ink, true);
  addText(s, "用户 A  ·  作品评论互动", 512, 280, 400, 24, 16, C.cyan, true);
  addBox(s, 512, 326, 580, 72, C.bg, 12, C.line);
  addText(s, "对方：请问具体怎么了解？", 536, 350, 420, 24, 16, C.ink, false);
  addBox(s, 600, 424, 492, 72, C.panel3, 12, C.cyan);
  addText(s, "我方：您好，可以根据您的地区和需求为您介绍。", 624, 448, 440, 24, 15, C.ink, false);
  pill(s, "打开浏览器回复", 512, 570, 146, C.blue);
  footer(s, 15);
}

// 16 account content
{
  const s = base(); title(s, "15 · 账号信息与内容生成", "账号信息是发布前的内容资产入口", "选择平台和账号后，读取已发布内容、素材、点赞、评论区与作品详情。");
  addBox(s, 72, 204, 494, 326, C.panel, 18, C.line);
  addText(s, "账号信息", 104, 236, 260, 26, 20, C.ink, true);
  bullet(s, "平台 → 账号 → 同步内容", 106, 292, 390, C.blue);
  bullet(s, "展示视频 / 图文详情", 106, 342, 390, C.cyan);
  bullet(s, "读取点赞数和评论内容", 106, 392, 390, C.green);
  bullet(s, "识别昵称，避免数字 ID 代替", 106, 442, 390, C.amber);
  addBox(s, 618, 204, 590, 326, C.panel2, 18, C.line);
  addText(s, "内容生成", 650, 236, 260, 26, 20, C.ink, true);
  addText(s, "选题 → 标题 → 正文 → 素材 → 平台版本", 650, 298, 480, 30, 18, C.cyan, true);
  addRule(s, 650, 354, 500, C.line, 1);
  bullet(s, "生成内容保留完整原文", 650, 386, 440, C.blue);
  bullet(s, "编辑后以最新本地版本发布", 650, 434, 440, C.green);
  bullet(s, "支持一键导入发布中心", 650, 482, 440, C.amber);
  footer(s, 16);
}

// 17 settings
{
  const s = base(); title(s, "16 · 设置、诊断与日志", "把环境问题变成可定位的问题", "BitBrowser、LLM、更新器、链路测试和操作日志集中管理。");
  addBox(s, 72, 198, 346, 338, C.panel, 18, C.line);
  addText(s, "连接设置", 104, 230, 220, 26, 20, C.ink, true);
  bullet(s, "识别 BitBrowser 端口", 106, 286, 280, C.blue);
  bullet(s, "填写 LLM 服务商 / 地址 / Key", 106, 336, 280, C.cyan);
  bullet(s, "检测 API 连通性", 106, 386, 280, C.green);
  bullet(s, "同步服务器可暂时留空", 106, 436, 280, C.amber);
  addBox(s, 466, 198, 346, 338, C.panel2, 18, C.line);
  addText(s, "诊断能力", 498, 230, 220, 26, 20, C.ink, true);
  bullet(s, "逐账号检查五个平台", 500, 286, 280, C.blue);
  bullet(s, "刷新到初始页面再检测", 500, 336, 280, C.cyan);
  bullet(s, "识别登录 / 验证 / 入口", 500, 386, 280, C.green);
  bullet(s, "随机抽取已采集数据验证", 500, 436, 280, C.amber);
  addBox(s, 860, 198, 348, 338, C.panel3, 18, C.line);
  addText(s, "运行日志", 892, 230, 220, 26, 20, C.ink, true);
  bullet(s, "记录调用、点击、反馈和异常", 894, 286, 280, C.cyan, 14);
  bullet(s, "正常 / 警告 / 错误三色", 894, 336, 280, C.green, 14);
  bullet(s, "每小时自动保存到本地", 894, 386, 280, C.amber, 14);
  bullet(s, "支持导出，便于复现问题", 894, 436, 280, C.blue, 14);
  footer(s, 17);
}

// 18 operating checklist
{
  const s = base(); title(s, "17 · 标准操作清单", "把每次使用都做成可复现的动作", "首次部署、日常采集、互动回复和版本升级都遵循相同的检查顺序。");
  addBox(s, 72, 190, 520, 360, C.panel, 18, C.line);
  addText(s, "日常工作", 104, 222, 240, 26, 20, C.ink, true);
  bullet(s, "① 检查账号状态和浏览器窗口", 106, 278, 440, C.blue);
  bullet(s, "② 选择关键词组并创建任务", 106, 326, 440, C.cyan);
  bullet(s, "③ 观察搜索、评论和入库数量", 106, 374, 440, C.green);
  bullet(s, "④ 在线索中心筛选并加入互动", 106, 422, 440, C.amber);
  bullet(s, "⑤ 模拟确认后再开启真实发送", 106, 470, 440, C.red);
  addBox(s, 640, 190, 568, 360, C.panel2, 18, C.line);
  addText(s, "出现异常时", 672, 222, 240, 26, 20, C.ink, true);
  bullet(s, "先看任务状态和右侧日志", 674, 278, 470, C.blue);
  bullet(s, "确认是否停留在目标平台页面", 674, 326, 470, C.cyan);
  bullet(s, "人工验证后点“继续”，不要点“开始”", 674, 374, 470, C.amber);
  bullet(s, "核对目标评论、输入框、发送按钮", 674, 422, 470, C.green);
  bullet(s, "导出日志和任务包，再进行复现", 674, 470, 470, C.red);
  addText(s, "第一版完", 72, 598, 300, 28, 18, C.cyan, true);
  addText(s, "后续可在此基础上继续补充真实消息中心、员工协作和发布截图。", 270, 598, 780, 28, 15, C.muted, false);
  footer(s, 18);
}

await fs.mkdir(TMP_DIR, { recursive: true });
const candidatePath = path.join(TMP_DIR, "candidate-manual-v1.pptx");
await (await PresentationFile.exportPptx(presentation)).save(candidatePath);
for (let i = 0; i < presentation.slides.items.length; i += 1) {
  const slide = presentation.slides.items[i];
  const preview = await presentation.export({ slide, format: "png", scale: 1 });
  await fs.writeFile(path.join(TMP_DIR, `draft-slide-${i + 1}.png`), new Uint8Array(await preview.arrayBuffer()));
}
console.log(JSON.stringify({ candidatePath, slideCount: presentation.slides.items.length, font: family }, null, 2));
