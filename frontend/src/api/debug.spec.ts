import { afterEach, describe, expect, it, vi } from 'vitest'
import { parseFrame, streamTurn, useTurnAbort, type TurnHandlers } from './debug'

function sse(frames: string[]): ReadableStream<Uint8Array> {
  const encoder = new TextEncoder()
  return new ReadableStream({
    start(controller) {
      for (const frame of frames) controller.enqueue(encoder.encode(frame))
      controller.close()
    },
  })
}

function recorder(): {
  handlers: TurnHandlers
  events: string[]
} {
  const events: string[] = []
  const handlers: TurnHandlers = {
    onStart: (body) => events.push(`start:${body.conversation_id}`),
    onTrace: (body) => events.push(`trace:${body.decision}`),
    onCitations: (body) => events.push(`citations:${body.length}`),
    onDelta: (text) => events.push(`delta:${text}`),
    onEnd: (body) => events.push(`end:${body.answer}`),
    onError: (body) => events.push(`error:${body.code}`),
  }
  return { handlers, events }
}

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('parseFrame', () => {
  it('拆出 event 与 JSON data', () => {
    expect(parseFrame('event: delta\ndata: {"text":"你"}\n')).toEqual({
      event: 'delta',
      data: { text: '你' },
    })
  })

  it('注释帧（心跳）返回 null', () => {
    expect(parseFrame(': ping')).toBeNull()
    expect(parseFrame('')).toBeNull()
  })

  it('非 JSON 的 data 原样返回', () => {
    expect(parseFrame('event: raw\ndata: not-json')).toEqual({ event: 'raw', data: 'not-json' })
  })

  it('多行 data 用换行拼接', () => {
    expect(parseFrame('event: x\ndata: {"a":1,\ndata: "b":2}')).toEqual({
      event: 'x',
      data: { a: 1, b: 2 },
    })
  })
})

describe('streamTurn', () => {
  it('按帧顺序回调 start / trace / citations / delta / end', async () => {
    const { handlers, events } = recorder()
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({
        ok: true,
        body: sse([
          'event: start\ndata: {"conversation_id":"c1","message_id":"m1"}\n\n',
          'event: trace\ndata: {"decision":"answer"}\n\n',
          'event: citations\ndata: {"citations":[{"n":1},{"n":2}]}\n\n',
          'event: delta\ndata: {"text":"你好"}\n\n',
          ': ping\n\n',
          'event: delta\ndata: {"text":"，世界"}\n\n',
          'event: end\ndata: {"answer":"你好，世界","latency_ms":12}\n\n',
        ]),
      }),
    )

    await streamTurn('app1', 'tok', { question: '你好' }, handlers)

    expect(events).toEqual([
      'start:c1',
      'trace:answer',
      'citations:2',
      'delta:你好',
      'delta:，世界',
      'end:你好，世界',
    ])
    const call = (fetch as unknown as ReturnType<typeof vi.fn>).mock.calls[0]
    expect(call[0]).toBe('/api/v1/admin/apps/app1/debug/chat')
    expect(call[1].headers.Authorization).toBe('Bearer tok')
    expect(JSON.parse(call[1].body)).toEqual({ question: '你好' })
  })

  it('把 conversation_id 带进后续轮次', async () => {
    const fetchMock = vi.fn().mockResolvedValue({ ok: true, body: sse([]) })
    vi.stubGlobal('fetch', fetchMock)
    const { handlers } = recorder()

    await streamTurn('app1', 'tok', { question: '继续', conversation_id: 'c9' }, handlers)

    expect(JSON.parse(fetchMock.mock.calls[0][1].body).conversation_id).toBe('c9')
  })

  it('data: 与 event: 被拆成多行时仍能解析（分块到达）', async () => {
    const { handlers, events } = recorder()
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({
        ok: true,
        body: sse(['event: del', 'ta\ndata: {"te', 'xt":"分块"}\n\n']),
      }),
    )

    await streamTurn('a', 't', { question: 'q' }, handlers)

    expect(events).toEqual(['delta:分块'])
  })

  it('非 2xx 时回调 onError 并带上后端 code', async () => {
    const { handlers, events } = recorder()
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({
        ok: false,
        status: 422,
        text: async () =>
          JSON.stringify({ error: { code: 'VALIDATION_ERROR', message: '调试参数校验失败' } }),
      }),
    )

    await streamTurn('a', 't', { question: 'q' }, handlers)

    expect(events).toEqual(['error:VALIDATION_ERROR'])
  })

  it('错误体不是 JSON 时退回 HTTP 状态码', async () => {
    const { handlers, events } = recorder()
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({ ok: false, status: 500, text: async () => '<html>' }),
    )

    await streamTurn('a', 't', { question: 'q' }, handlers)

    expect(events).toEqual(['error:HTTP_ERROR'])
  })
})

describe('useTurnAbort', () => {
  it('start 后 running=true，stop 中止并复位', () => {
    const { running, start, stop } = useTurnAbort()
    const controller = start()
    expect(running.value).toBe(true)
    expect(controller.signal.aborted).toBe(false)

    stop()
    expect(controller.signal.aborted).toBe(true)
    expect(running.value).toBe(false)
  })
})
