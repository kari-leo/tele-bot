import z from '@deepseek-ai/schemastery'
export interface Config { adminUserIds: string[]; resourcesJson: string; baseLinksJson: string; apiBaseUrl: string; dataDirectory: string; confirmAll: boolean }
export const Config = z.object({ adminUserIds: z.array(z.string()).default([]), resourcesJson: z.string().default('[]'), baseLinksJson: z.string().default('[]'),
  apiBaseUrl: z.string().default('https://open.feishu.cn'), dataDirectory: z.string().default(''), confirmAll: z.boolean().default(true) })
