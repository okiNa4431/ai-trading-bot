import fs from 'node:fs/promises'
const url=process.env.SUPABASE_URL || ''
const key=process.env.SUPABASE_PUBLISHABLE_KEY || ''
if (url && (new URL(url).protocol!=='https:' || new URL(url).pathname!=='/')) throw Error('Invalid public endpoint')
if (key && !key.startsWith('sb_publishable_')) {
  let payload
  try { payload=JSON.parse(Buffer.from(key.split('.')[1],'base64url').toString()) } catch { throw Error('Invalid publishable key') }
  if (payload.role!=='anon') throw Error('Only publishable/anon keys are allowed in the public build')
}
await fs.mkdir('public',{recursive:true})
await fs.writeFile('public/runtime-config.json',JSON.stringify({url,key}))
console.log(`Public read configuration: ${url&&key?'configured':'not configured'}`)
