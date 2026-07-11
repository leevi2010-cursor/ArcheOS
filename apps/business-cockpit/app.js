const tenants = {
  amazon: {
    name: "Amazon US Store",
    kpis: [
      ["总销售额", "$1,234,567", "↑ 12.5%", true],
      ["毛利润", "$234,567", "↑ 8.3%", true],
      ["现金流（30天）", "$123,456", "↑ 15.2%", true],
      ["ACoS", "18.7%", "↑ 2.1pp", false],
      ["库存周转天数", "58 天", "↓ 6 天", true],
      ["高风险预警", "3 项", "↑ 1 项", false]
    ],
    strategy: {
      primary: "现金流安全",
      primaryDesc: "确保现金流稳定，保障库存与运营安全。",
      secondary: "利润增长",
      secondaryDesc: "在毛利率不低于 15% 的前提下增长利润。",
      cycle: "2026 Q3",
      owner: "经营负责人",
      constraints: ["库存周转天数不超过 90 天", "单日广告预算调整不得超过 ±20%", "高风险动作需经营负责人审批"]
    },
    agents: [
      ["广告优化师", "广告投放优化", "$45,678", 82],
      ["选品分析师", "选品与机会发现", "$32,456", 74],
      ["库存管理师", "库存健康管理", "$18,765", 68],
      ["Listing 优化师", "Listing 优化", "$12,345", 62],
      ["客服助手", "客户服务与回复", "$5,678", 55]
    ],
    risks: [
      ["现金流风险", "现金余额预计将在 6 天后低于安全线。", "高"],
      ["库存积压风险", "SKU B07XYZ 库存周转天数已达 85 天。", "中"],
      ["广告 ACoS 异常", "关键词 running shoes 的 ACoS 上升异常。", "中"]
    ],
    proposals: [
      ["增强广告优化师的现金流约束感知", "当前广告优化建议未充分考虑现金流约束，建议在 Skill 中加入现金流上下文。", "预计收益：$8,000+/月"],
      ["优化库存预测模型的季节性修正", "库存预测在季节性波动期间偏高，建议引入季节性因子。", "预计收益：$3,200+/月"],
      ["调整 Listing 优化评价权重", "当前评价更偏重关键词密度，建议提高转化率权重。", "预计收益：$1,200+/月"]
    ],
    skills: [
      ["广告优化", "稳定", 87, "2026-06-10"],
      ["选品分析", "稳定", 82, "2026-06-12"],
      ["库存预测", "实验中", 63, "2026-06-18"],
      ["财务预测", "实验中", 58, "2026-06-20"],
      ["市场情报", "未覆盖", 0, "—"]
    ]
  },
  showroom: {
    name: "黄鸿儒展厅",
    kpis: [
      ["有效客流", "386", "↑ 9.4%", true],
      ["成交金额", "¥1,086,000", "↑ 11.8%", true],
      ["毛利润", "¥284,000", "↑ 7.1%", true],
      ["到店转化率", "24.6%", "↓ 1.2pp", false],
      ["平均跟进时长", "2.8 天", "↓ 0.7 天", true],
      ["高风险预警", "2 项", "持平", true]
    ],
    strategy: {
      primary: "客户转化质量",
      primaryDesc: "提升有效客户识别和持续跟进质量，而非单纯追求客流。",
      secondary: "组织能力沉淀",
      secondaryDesc: "把店长、导购和客户运营经验沉淀为可复用方法。",
      cycle: "2026 Q3",
      owner: "黄鸿儒 / 项目负责人",
      constraints: ["不得牺牲客户体验换取短期成交", "高价值客户必须由人类负责人参与", "新增流程先小范围验证再推广"]
    },
    agents: [
      ["客户运营助手", "线索分层与跟进", "¥126,000", 78],
      ["店长助手", "经营复盘与任务推进", "¥88,000", 71],
      ["导购教练", "话术与案例辅导", "¥65,000", 66],
      ["内容助手", "内容计划与素材整理", "¥42,000", 59]
    ],
    risks: [
      ["高价值客户流失", "7 位高意向客户超过 3 天未完成关键跟进。", "高"],
      ["转化率下降", "客流增加但到店转化率连续两周下降。", "中"]
    ],
    proposals: [
      ["调整客户分层规则", "当前模型过度依赖预算字段，建议加入空间进度和决策链完整度。", "预计提升转化率 2–4pp"],
      ["增加店长周复盘 Skill", "将客户、人员、陈列和活动问题统一进入周复盘。", "预计减少遗漏 30%"],
      ["建立导购案例评价集", "将优秀与失败案例结构化，用于持续校准导购教练。", "提升辅导一致性"]
    ],
    skills: [
      ["客户分层", "稳定", 81, "2026-06-09"],
      ["跟进建议", "实验中", 69, "2026-06-15"],
      ["店长复盘", "实验中", 61, "2026-06-20"],
      ["导购辅导", "实验中", 57, "2026-06-21"],
      ["活动归因", "未覆盖", 0, "—"]
    ]
  }
};

const el = id => document.getElementById(id);
const spark = (positive = true) => `<svg class="spark" viewBox="0 0 120 28"><polyline fill="none" stroke="${positive ? '#356df3' : '#e64646'}" stroke-width="2" points="0,22 12,18 24,20 36,12 48,15 60,7 72,14 84,5 96,11 108,4 120,9"/></svg>`;

function render(key) {
  const data = tenants[key];
  el("sidebarTenant").textContent = data.name;
  el("kpiGrid").innerHTML = data.kpis.map(([label, value, delta, good]) => `
    <article class="kpi"><div class="label">${label}</div><div class="value">${value}</div><div class="delta ${good ? 'positive' : 'negative'}">${delta}</div>${spark(good)}</article>`).join("");

  const s = data.strategy;
  el("strategyContent").innerHTML = `
    <div class="strategy-block"><strong>首要目标 · ${s.primary}</strong><p>${s.primaryDesc}</p></div>
    <div class="strategy-block secondary"><strong>次级目标 · ${s.secondary}</strong><p>${s.secondaryDesc}</p></div>
    <div class="meta-list"><div><span>策略周期</span><strong>${s.cycle}</strong></div><div><span>最终裁决者</span><strong>${s.owner}</strong></div></div>
    <div class="constraints"><strong>重大约束</strong><ul>${s.constraints.map(c => `<li>${c}</li>`).join("")}</ul></div>`;

  el("agentTable").innerHTML = data.agents.map(([name, role, value, rate]) => `
    <div class="table-row"><div><strong>${name}</strong><small>${role}</small></div><div><strong>${value}</strong><small>估算贡献</small></div><div>${rate}%<div class="progress"><span style="width:${rate}%"></span></div></div></div>`).join("");

  el("riskList").innerHTML = data.risks.map(([title, desc, severity]) => `
    <div class="list-item risk"><h3>${severity === '高' ? '🔥' : '⚠️'} ${title}</h3><p>${desc}</p><div class="item-footer"><span class="${severity === '高' ? 'severity-high' : 'severity-mid'}">影响：${severity}</span><button class="approve-btn">处理</button></div></div>`).join("");

  el("proposalList").innerHTML = data.proposals.map(([title, desc, impact], index) => `
    <div class="list-item"><h3>${title}</h3><p>${desc}</p><div class="item-footer"><span>${impact}</span><button class="approve-btn" data-proposal="${index}">查看提案</button></div></div>`).join("");

  el("skillTable").innerHTML = data.skills.map(([name, status, health, date]) => `
    <div class="table-row"><div><strong>${name}</strong><small>Capability View</small></div><div><strong>${status}</strong><small>最近变更 ${date}</small></div><div>${health ? `${health}%<div class="progress"><span style="width:${health}%"></span></div>` : '—'}</div></div>`).join("");
}

el("tenantSelect").addEventListener("change", event => render(event.target.value));

const dialog = el("strategyDialog");
el("editStrategyBtn").addEventListener("click", () => {
  const s = tenants[el("tenantSelect").value].strategy;
  el("primaryGoalInput").value = s.primary;
  el("secondaryGoalInput").value = s.secondary;
  el("constraintInput").value = s.constraints.join("\n");
  dialog.showModal();
});

el("saveStrategyBtn").addEventListener("click", event => {
  event.preventDefault();
  const key = el("tenantSelect").value;
  const s = tenants[key].strategy;
  s.primary = el("primaryGoalInput").value.trim() || s.primary;
  s.secondary = el("secondaryGoalInput").value.trim() || s.secondary;
  s.constraints = el("constraintInput").value.split("\n").map(x => x.trim()).filter(Boolean);
  dialog.close();
  render(key);
});

document.addEventListener("click", event => {
  const button = event.target.closest("[data-proposal]");
  if (!button) return;
  button.textContent = "已进入审批队列";
  button.disabled = true;
});

render("amazon");
