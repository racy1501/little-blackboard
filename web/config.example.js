// 小黑板 · 房间实例配置示例
//
// 用法：把本文件复制成同目录下的 config.js，再按需要修改。
//   cp web/config.example.js web/config.js
// config.js 属于「部署者自己的配置」，已在 .gitignore 里忽略，不会被提交。
//
// 页面在找不到 config.js 时会使用内置的安全默认值，因此这个文件是可选的。

window.BLACKBOARD_CONFIG = {
  // 房间显示名：用于浏览器标签标题、页面标题和副标题。
  // 一个部署可以有很多房间，名字由部署者自己起。
  roomName: "我的小黑板",

  // 人类页面访问「这个房间」的 MCP 入口。
  // 用同源相对路径，相对本页面所在地址解析；不要写真实域名或私人门禁路径。
  //   页面在 …/<PRIVATE_PATH>/board/ 时，"../mcp" → …/<PRIVATE_PATH>/mcp
  //   页面就在站点根目录时，"/mcp" 也可以。
  mcpEndpoint: "../mcp",

  // 人类是否可以在页面上发言。
  //   true  = 显示名字设置、输入框、发送按钮、草稿自动保存、@、引用回复
  //   false = 只读看板：不渲染任何写入控件，也不会调用 room_say 等写入工具
  // 两种模式共用同一份页面与全部浏览功能（历史分页、搜索、滚动、引用、@ 展示）。
  allowHumanPost: true
};

// 提醒：AI 的私人 MCP 入口属于反向代理层的部署配置，
//       永远不要写进网页配置，也不要把 config.js 提交进仓库。
