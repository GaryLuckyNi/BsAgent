import { createRoot } from 'react-dom/client'
import { ResearchMessageItem } from '@/components/research/research-message-item'
import '@/styles.css'

createRoot(document.getElementById('root')!).render(<div className="app-shell" style={{ background: 'white' }}>
  <header className="topbar"><div className="brand"><b>InvestWise</b><i>正式组件验收 · 示例资料</i></div></header>
  <main className="conversation" style={{ overflow: 'auto' }}><div className="conversation-content">
    <ResearchMessageItem onCitation={() => {}} message={{
      id: 'question-check', role: 'user', status: 'complete', createdAt: '',
      content: '请分析公司的盈利变化及现金流质量。'
    }} />
    <ResearchMessageItem onCitation={() => {}} message={{
      id: 'component-check', role: 'assistant', status: 'complete', createdAt: '',
      evidence: [{ id: 'source_7', index: 7, title: '年度报告与主要财务数据 · 预览示例',
        url: 'https://finance.sina.com.cn/', publishedAt: '2025-04-29',
        summary: '此处复用已确认的设计示例，展示正式组件对来源标题、摘要、日期和网站图标的呈现。' },
        { id: 'source_12', index: 12, title: '财务报表附注与现金流说明 · 示例', url: 'https://www.cninfo.com.cn/', publishedAt: '2025-04-29', summary: '示例摘要：经营活动现金流、应收账款与存货项目的变化。' },
        { id: 'source_13', index: 13, title: '公司年度披露与主要财务数据 · 示例', url: 'https://www.sse.com.cn/disclosure/', summary: '示例摘要：年度披露中的主要会计数据与财务指标。' },
        { id: 'source_14', index: 14, title: '产业需求与产品结构变化 · 示例', url: 'https://finance.sina.com.cn/stock/', summary: '示例摘要：产业新闻为公司经营分析提供背景，不代表正文一定引用。' },
        { id: 'source_15', index: 15, title: '行业景气与竞争环境 · 示例', url: 'https://www.stcn.com/', summary: '示例摘要：行业供需变化与竞争格局。' }],
      content: '## 数据确认与来源\n\n同期归母净利润为 **−2.66 亿元**（−26,575.71 万元），上年为盈利 6,575.25 万元。2024 年末归属于上市公司股东的净资产为 **73.38 亿元**。依据（[7](https://finance.sina.com.cn/)）。\n\n**关键变化在于利润与现金流的方向不同。** 普通正文保持正常字重，让结论、金额和重要概念自然突出。\n\n## 📜 计算口径与解读\n\n净利率采用 **归母净利润 ÷ 营业收入** 计算。标题中的 emoji 来自原文，保持原始颜色。\n\n### 阅读层次\n\n- **数据事实：** 保留主体、期间、金额与单位。\n- **分析判断：** 基于已取得的事实解释含义。\n\n历史引用只有地址时也能打开预览（[9](https://www.sse.com.cn/)）。\n\n> 本页使用正式聊天组件，仅提供视觉与交互验收，不发送研究请求。'
    }} />
  </div></main>
</div>)
