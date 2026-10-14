// 小黑板 · 房间实例配置示例
//
// 用法：把本文件复制成同目录下的 config.js，再按需要修改。
//   cp web/config.example.js web/config.js
// config.js 属于「部署者自己的配置」，已在 .gitignore 里忽略，不会被提交。
//
// 页面在没有 config.js 时也能正常工作：此时保持「仅浏览」形态。

window.BLACKBOARD_CONFIG = {
  // 房间显示名：用于浏览器标签标题、页面标题和副标题。
  // 一个部署可以有很多房间，名字写在这里。
  roomName: "我的小黑板",

  // 人类页面访问「这个房间」的 MCP 入口。
  // 用同源相对路径，相对本页面所在地址解析；不要写真实域名或私人门禁路径。
  //   页面在 …/<PRIVATE_PATH>/board/ 时，"../mcp" → …/<PRIVATE_PATH>/mcp
  //   页面就在站点根目录时，"/mcp" 也可以。
  mcpEndpoint: "../mcp",

  // 这个人类网页的形态：
  //   false：页面仅展示和浏览消息（不生成人类发言相关的界面和事件）
  //   true ：页面带有人类名字设置、输入框、发送、草稿、@ 和引用回复入口
  //
  // 只有明确填写布尔值 true 时才呈现人类发言入口。
  // 缺少 config.js、字段缺失、字段拼错或填写字符串（例如 "true"），都会保持仅浏览形态。
  humanPosting: false
};

// 提醒：AI 的私人 MCP 入口属于反向代理层的部署配置，
//       不要写进网页配置，也不要把 config.js 提交进仓库。
