# 第三方静态依赖（vendored）

工作台的样式与图表依赖原本从 `https://modao.cc/...` 加载。浏览器对这类跨源
`no-cors` 脚本请求启用了 **ORB（Opaque Response Blocking）**，三个脚本全部被
`net::ERR_BLOCKED_BY_ORB` 拦掉 → Tailwind 不生成样式、Iconify 不出图标、
ECharts 不出图，页面退化成**无样式的纵向文本堆叠**。

因此把三个依赖**下载到本目录**，由本项目自己的静态服务同源提供：

| 文件 | 版本 | 许可证 | 来源 | 版权 |
| --- | --- | --- | --- | --- |
| `tailwindcss.js` | 3.4.3（Tailwind Play CDN 构建） | MIT | https://modao.cc/agent-py/static/source/js/tailwindcss.js （上游 https://cdn.tailwindcss.com） | Tailwind Labs, Inc. |
| `echarts.min.js` | 5.4.4 | Apache-2.0 | https://modao.cc/agent-py/static/source/js/echarts.min.js （上游 https://github.com/apache/echarts） | The Apache Software Foundation |
| `iconify-icon.min.js` | 1.0.7（Web Component） | MIT | https://modao.cc/agent-py/static/source/js/iconify-icon.min.js （上游 https://github.com/iconify/iconify） | Iconify |

- 三个文件均为**上游原始产物，未改动**（可用 `shasum` 与上游比对）。
- Tailwind Play CDN 在运行时扫描 DOM 生成样式，靠内置 MutationObserver 跟随动态 DOM，
  所以**离线同源**提供后行为与原来一致。
- 三个文件都**不引用任何外部字体或外部资源**，页面不再产生跨源请求。

如果上游发布了新版本，替换本目录文件后**必须**同步更新 `workbench.html` 里的
`?v=` 版本参数，否则浏览器会继续用旧缓存。
