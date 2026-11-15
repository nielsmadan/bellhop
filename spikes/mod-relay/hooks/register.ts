import type { Register } from 'claude-code'

export const register: Register = (on) => {
  on('session.start', async ($, e, next) => {
    const started = await next(e)
    void (async () => {
      const relay = $.process.spawn({
        argv: ['/bin/sh', '-c', 'exec "$BELLHOP_SPIKE_PYTHON" -I "$BELLHOP_SPIKE_RELAY" serve "$BELLHOP_SPIKE_SOCKET"'],
      })
      for await (const { text } of relay) $.ui.log(text, { to: 'debug' })
    })()
    return started
  })
}
