import { useState } from 'react'
import type { Session } from './api'

type Trade = {id:string;revision:number;author:string;status:string;offers:unknown[];confirmed:string[]}
export function TradePanel({session,trades,collections,onRefresh}:{session:Session;trades:Trade[];collections:Record<string,number[]>;onRefresh:()=>void}) {
  const [id,setId]=useState('exchange-1')
  const [offers,setOffers]=useState('[]')
  const [error,setError]=useState('')
  const send=async(action:string,tradeId:string,revision?:number)=>{
    try{
      const body={action,id:tradeId,revision,offers:action==='create'||action==='edit'?JSON.parse(offers):undefined}
      const response=await fetch(`/games/${session.gameId}/trades?token=${encodeURIComponent(session.token)}`,{
        method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)})
      const result=await response.json();if(!response.ok)throw new Error(result.detail)
      setError('');onRefresh()
    }catch(e){setError(String(e))}
  }
  return <section className="card"><h2>公开卡牌交换</h2><h3>当前收藏与玩家编号</h3><pre>{JSON.stringify(collections,null,2)}</pre>
    <label>交换单号<input value={id} onChange={e=>setId(e.target.value)}/></label>
    <label>交换内容（from、to 为玩家编号，card_id 为公开卡编号）<textarea value={offers} onChange={e=>setOffers(e.target.value)}/></label>
    <button onClick={()=>send('create',id)}>提出交换</button><p>{error}</p>
    {trades.map(t=><div key={t.id}><p>{t.id} · 版本 {t.revision} · {t.status}</p><pre>{JSON.stringify(t.offers,null,2)}</pre>
      <p>已确认：{t.confirmed.join(', ')}</p>
      <button disabled={t.status!=='open'} onClick={()=>send('confirm',t.id,t.revision)}>确认当前内容</button>
      {t.author===session.playerId&&<><button onClick={()=>send('edit',t.id,t.revision)}>更新为上方内容</button><button onClick={()=>send('cancel',t.id,t.revision)}>撤销</button></>}
    </div>)}
  </section>
}
