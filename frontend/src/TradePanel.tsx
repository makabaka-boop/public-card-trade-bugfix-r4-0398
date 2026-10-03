import { useState } from 'react'
import type { Session, TradeView } from './api'

export function TradePanel({session,trades,collections,onRefresh}:{session:Session;trades:TradeView[];collections:Record<string,number[]>;onRefresh:()=>void}) {
  const [id,setId]=useState('exchange-1')
  const [offers,setOffers]=useState('[]')
  const [error,setError]=useState('')
  const seatOf = (playerId:string) => {
    // Collections/trades use player ids; label them by seat where possible.
    return playerId === session.playerId ? '你' : `玩家 ${playerId.slice(0,4)}`
  }
  const send=async(action:string,tradeId:string,revision?:number)=>{
    try{
      const parsed = action==='create'||action==='edit' ? JSON.parse(offers) : undefined
      const body={action,id:tradeId,revision,offers:parsed}
      const response=await fetch(`/games/${session.gameId}/trades?token=${encodeURIComponent(session.token)}`,{
        method:'POST','headers':{'Content-Type':'application/json'},body:JSON.stringify(body)})
      const result=await response.json();if(!response.ok)throw new Error(errorText(result.detail))
      setError('');onRefresh()
    }catch(e){setError(String(e))}
  }
  return <section className="card"><h2>公开卡牌交换</h2><h3>当前收藏（只含已公开卡牌）</h3><pre>{JSON.stringify(collections,null,2)}</pre>
    <label>交换单号<input value={id} onChange={e=>setId(e.target.value)}/></label>
    <label>交换内容（from、to 为玩家编号/ID，card_id 为已公开卡编号）<textarea value={offers} onChange={e=>setOffers(e.target.value)}/></label>
    <button onClick={()=>send('create',id)}>提出交换</button><p className="error">{error}</p>
    {trades.map(t=>{
      const mine = t.participants.includes(session.playerId)
      const iConfirmed = t.confirmed.includes(session.playerId)
      return <div key={t.id}><p>{t.id} · 版本 {t.revision} · {statusText(t.status)}{t.author===session.playerId?' · 我提出的':''}</p>
        <pre>{JSON.stringify(t.offers,null,2)}</pre>
        <p>参与者：{t.participants.map(seatOf).join(', ')}</p>
        <p>已确认当前版本：{t.confirmed.length===0?'（无）':t.confirmed.map(seatOf).join(', ')}</p>
        <button disabled={t.status!=='open'||!mine||iConfirmed} onClick={()=>send('confirm',t.id,t.revision)}>
          {iConfirmed?'已确认当前版本':'确认当前内容'}
        </button>
        {t.author===session.playerId&&<><button disabled={t.status!=='open'} onClick={()=>send('edit',t.id,t.revision)}>更新为上方内容（需重新确认）</button><button disabled={t.status!=='open'} onClick={()=>send('cancel',t.id,t.revision)}>撤销</button></>}
      </div>})}
  </section>
}

function statusText(status: TradeView['status']): string {
  return ({open:'待全部确认',committed:'已成交',cancelled:'已撤销'} as const)[status]
}

function errorText(code: unknown): string {
  return (
    {
      invalid_trade:'交换内容无效（需 2–4 名参与者，每人都有送出和收到的牌）',
      card_unavailable:'卡牌不可用：未公开、已不属于送出者，或在同一单中重复',
      revision_mismatch:'版本已变更，请按当前内容重新确认',
      not_participant:'你不是该交换的参与者',
      not_author:'只有提出者可以修改或撤销',
      trade_not_found:'交换单不存在',
      unauthorized:'令牌无效',
    } as Record<string,string>
  )[String(code)] ?? String(code)
}
