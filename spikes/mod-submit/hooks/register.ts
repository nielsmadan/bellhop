import type { Register } from 'claude-code'

export const register: Register = (on) => {
  on('session.start', async ($, e, next) => {
    const started = await next(e)
    void (async () => {
      const listener = $.process.spawn({
        argv: ['/bin/sh', '-c', 'exec "$BELLHOP_SPIKE_PYTHON" -I "$BELLHOP_SPIKE_RELAY" print "$BELLHOP_SPIKE_SOCKET"'],
      })
      let pending = ''
      for await (const { stream, text } of listener) {
        if (stream !== 'stdout') continue
        pending += text
        const lines = pending.split('\n')
        pending = lines.pop() ?? ''
        for (const line of lines.filter((l) => l.trim())) {
          const message = JSON.parse(line) as { text: string }
          void $.prompt.submit({ text: message.text, asUser: true })
        }
      }
    })()
    return started
  })
}
