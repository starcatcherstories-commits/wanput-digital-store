
'use client'
import { useState } from 'react'
const PRODUCTS = [
  { id:1, name:'คลังใบงาน 1,000+ หน้า แม่ไทยใช้จริง', price:159, drive:'https://drive.google.com/drive/folders/PUT_YOUR_RAW_LINK_HERE', cover:'📚' },
]
export default function Page(){
  const [paid,setPaid]=useState(false)
  return (
    <div style={{fontFamily:'sans-serif', maxWidth:800, margin:'40px auto', padding:20}}>
      <h1 style={{fontSize:32, fontWeight:900}}>คลังใบงาน 1,000+ หน้า ที่แม่ไทยใช้จริง</h1>
      <p>ลูกเลิกจอใน 7 วัน - ตัวอย่างร้านที่ deploy บน Vercel สำเร็จแล้ว</p>
      <div style={{border:'1px solid #ddd', borderRadius:12, padding:20, marginTop:20}}>
        <div style={{fontSize:48}}>📚</div>
        <div style={{fontWeight:700, marginTop:10}}>คลังใบงาน 1,000+ หน้า</div>
        <div>ราคา 159 บาท (จาก 500 บาท)</div>
        <button onClick={()=>setPaid(true)} style={{marginTop:15, background:'black', color:'white', padding:'10px 20px', borderRadius:999, border:'none', cursor:'pointer'}}>ซื้อเลย - ทดสอบหลังบ้าน</button>
        {paid && <div style={{marginTop:20, background:'#f0fdf4', padding:15, borderRadius:10}}>
          <div>✅ ชำระสำเร็จ! หลังบ้านทำงานแล้ว</div>
          <a href={PRODUCTS[0].drive} target='_blank' style={{display:'block', marginTop:10, background:'#16a34a', color:'white', textAlign:'center', padding:10, borderRadius:999, textDecoration:'none'}}>ดาวน์โหลด Raw File จาก Drive</a>
          <div style={{fontSize:12, color:'#666', marginTop:10}}>Raw file ต้องอยู่บน Google Drive ไม่ได้อยู่ใน index.html นะ - เปลี่ยนลิงก์ใน app/page.tsx</div>
        </div>}
      </div>
      <div style={{marginTop:40, background:'#f5f5f5', padding:20, borderRadius:12}}>
        <h3>วิธี Deploy ให้ไม่วนแค่ index.html</h3>
        <ol>
          <li>อย่าใช้ Vercel Drop แบบลากแค่ index.html</li>
          <li>อัพโฟลเดอร์นี้ทั้งโฟลเดอร์ขึ้น GitHub</li>
          <li>ใน Vercel เลือก Import Project - Framework ต้องเป็น Next.js ไม่ใช่ Other</li>
          <li>ใส่ Drive link ของ Raw file ใน app/page.tsx</li>
        </ol>
      </div>
    </div>
  )
}
