---
name: publish-source
description: Publish original Markdown files without copying their bodies through the model
---

用户要求发布本地文档原文时，优先使用 `blog_publish_from_file`，直接传入源文件
`source_path` 和简短元数据。不要为了发布而分页读完整正文，也不要把正文复制进工具参数。
需要理解内容或用户要求修改时，可读取相关页面；发布原文仍使用源文件入口。

发布到作品集项目时，先确认已有项目 slug，然后设置 `project`（例如 `telebot`）；
工具写入 `projectDocuments` 集合，正文完整保留。多个文档使用各自唯一的 `slug` 和 `order`。
用户明确要求“推出去、推送、发布到博客”已经授权 Git push，不要重复询问确认。
可以直接设置 `git_push_enabled=true`；发布多份文档时，优先使用
`blog_publish_files(documents=[全部要求的源文件和元数据...], project="telebot", git_push_enabled=true)`。
不得只处理历史记录中的最后一份或失败的一份。每次新请求都必须核对当前要求的全部源文件，
不能用旧的成功记录认定另外几份现在仍存在。批量入口会复用现有正文，并一次性提交、推送全部路径。
只有返回 `status=pushed` 且 `pushed_count=requested_count` 才能说明全部推送完成。
批量失败时根据 `files`、`written_count`、`pushed_count` 汇报实际结果。
需要只推送已有文件时使用 `blog_push(file_paths=[全部实际路径...])`。
用户只要求本地写入或明确不要推送时，保持 `git_push_enabled=false`。

同一正文再次发布会复用已有文件，不会因为更换 slug 生成副本；不要为了获取推送令牌
重新写入文档或添加 -v2、-2 等后缀。已有文件直接使用 `blog_push`，不要调用写入入口。
GitHub 网络失败后只重试同一文件的 push，最多重试一次；仍失败则报告具体原因并停止。
不要换 slug 或重新提交来绕过推送失败。只有工具明确返回“git push 已完成”才能说推送成功。
只返回文件路径表示已写入本地，不能说博客已上线。
没有该工具时按现有工具能力汇报限制。
