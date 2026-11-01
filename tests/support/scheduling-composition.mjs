/** Public rc.5 services normally supplied by the Web/Desktop bundle.
 * ACP deliberately omits them. Tests compose the official components unchanged.
 */
export const schedulingRows = [
  { id: 'telebot-test-workspace', name: '@deepseek-ai/dsh-workspace' },
  { id: 'telebot-test-connection', name: '@deepseek-ai/dsh-client-connection', config: { trustedHosts: [] } },
  { id: 'telebot-test-file-upload', name: '@deepseek-ai/dsh-client-file-upload' },
  { id: 'telebot-test-session-controller', name: '@deepseek-ai/dsh-api-session-controller', config: { nativeOpen: false } },
  { id: 'telebot-test-presets', name: '@deepseek-ai/dsh-agent-preset-registry', config: { default: 'standard' } },
  // ACP keeps its normal tools in the base host scope, unlike Web's scoped preset.
  { id: 'telebot-test-preset', name: '@deepseek-ai/dsh-agent-preset', config: { id: 'standard', plugins: [] } },
  { id: 'telebot-test-schedule', name: '@deepseek-ai/dsh-schedule' },
]
