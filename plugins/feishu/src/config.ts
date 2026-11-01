import z from '@deepseek-ai/schemastery'
import { Config as BusinessSchema, type Config as BusinessConfig } from './business/config.js'

export interface Config {
  configDirectory: string
  deploymentPort: number
  dedicatedIngress: boolean
  publicWebhookUrl: string
  businessEnabled: boolean
  business: BusinessConfig
  appId: string
  appSecretRef: string
  verificationTokenRef: string
  encryptKeyRef: string
  adminUserIds: string[]
  allowedChatIds: string[]
  webhookPath: string
  workspace: string
  dataDirectory: string
  provider: string
  model: string
  agentPreset: string
  apiBaseUrl: string
  apiTimeoutMs: number
  maxBodyBytes: number
  queueLimit: number
  maxConcurrentSessions: number
  pollMs: number
  leaseMs: number
  retryMs: number
  maxDeliveryAttempts: number
  actionTtlMs: number
  mergeMs: number
  maxMergeWaitMs: number
  streamProgress: boolean
}
export const Config = z.object({
  configDirectory: z.string().default(''),
  deploymentPort: z.number().min(1).max(65535).default(3000),
  dedicatedIngress: z.boolean().default(false),
  publicWebhookUrl: z.string().default(''),
  businessEnabled: z.boolean().default(true),
  business: BusinessSchema.default({}),
  mergeMs: z.number().min(0).max(10000).default(3000),
  maxMergeWaitMs: z.number().min(0).max(10000).default(10000),
  streamProgress: z.boolean().default(true),
  appId: z.string().default(''),
  appSecretRef: z.string().role('credential-ref').default('FEISHU_APP_SECRET'),
  verificationTokenRef: z.string().role('credential-ref').default('FEISHU_VERIFICATION_TOKEN'),
  encryptKeyRef: z.string().role('credential-ref').default('FEISHU_ENCRYPT_KEY'),
  adminUserIds: z.array(z.string()).default([]),
  allowedChatIds: z.array(z.string()).default([]),
  webhookPath: z.string().default('/feishu/webhook'),
  workspace: z.string().default(''),
  dataDirectory: z.string().default(''),
  provider: z.string().default(''),
  model: z.string().default(''),
  agentPreset: z.string().default(''),
  apiBaseUrl: z.string().default('https://open.feishu.cn'),
  apiTimeoutMs: z.number().min(100).max(120000).default(10000),
  maxBodyBytes: z.number().min(1024).max(2097152).default(1048576),
  queueLimit: z.number().min(1).max(10000).default(1000),
  maxConcurrentSessions: z.number().min(1).max(16).default(4),
  pollMs: z.number().min(25).max(5000).default(250),
  leaseMs: z.number().min(1000).max(120000).default(10000),
  retryMs: z.number().min(50).max(300000).default(5000),
  maxDeliveryAttempts: z.number().min(1).max(20).default(5),
  actionTtlMs: z.number().min(100).max(1800000).default(600000),
})
export function validateCallbacks(config: Config) {
  if (!Number.isSafeInteger(config.deploymentPort) || config.deploymentPort < 1 || config.deploymentPort > 65535) throw new Error('valid_deployment_port_required')
  if (['/api','/connect','/telebot'].some(reserved=>config.webhookPath===reserved||config.webhookPath.startsWith(reserved+'/'))) throw new Error('setup_path_is_reserved')
  if (config.publicWebhookUrl) {
    const url = new URL(config.publicWebhookUrl)
    if (url.protocol !== 'https:' || url.username || url.password || url.search || url.hash || url.pathname !== config.webhookPath) throw new Error('matching_https_public_callback_required')
  }
}
