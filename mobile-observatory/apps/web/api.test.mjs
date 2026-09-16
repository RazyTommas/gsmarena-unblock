import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';

const calls=[];
globalThis.fetch=async (url, options={})=>{
  calls.push({url,options});
  return { ok:true, status:200, json:async()=>({items:[]}) };
};
const { api, ApiError } = await import('./api.js');

test('filter values are encoded and empty values omitted', async()=>{
  await api.devices({maker:'Xiaomi / Redmi', android:'', max_android:16});
  assert.equal(calls.at(-1).url,'/api/v1/devices?maker=Xiaomi+%2F+Redmi&max_android=16');
});

test('acknowledgement is an explicit POST', async()=>{
  await api.acknowledge('event/42');
  assert.equal(calls.at(-1).url,'/api/v1/updates/event%2F42/acknowledge');
  assert.equal(calls.at(-1).options.method,'POST');
});

test('product detail and reverse silicon use encoded stable identifiers', async()=>{
  await api.productDetail('product/42');
  assert.equal(calls.at(-1).url,'/api/v1/products/product%2F42');
  await api.chipProducts({vendor:'Qualcomm',part:'Snapdragon 7+ Gen 2',cursor:200});
  assert.equal(calls.at(-1).url,'/api/v1/chips/products?vendor=Qualcomm&part=Snapdragon+7%2B+Gen+2&cursor=200');
  await api.productReleases({product:'product/42',region:'EEA',channel:'Stable Beta',cursor:50});
  assert.equal(calls.at(-1).url,'/api/v1/product-releases?product=product%2F42&region=EEA&channel=Stable+Beta&cursor=50');
});

test('non-success responses become typed API errors', async()=>{
  globalThis.fetch=async()=>({ok:false,status:503});
  await assert.rejects(api.health(), error=>error instanceof ApiError && error.status===503);
});

test('workflow controls have event-bound identifiers', async()=>{
  const source=await readFile(new URL('./app.js',import.meta.url),'utf8');
  for(const id of ['radarTabs','radarRegion','radarChange','exploreTabs','makerFilter','chipFilter','familyFilter','partFilter','androidFilter','regionFilter','supportFilter','exportView','themeSelect','modelQuery','saveConfig']) {
    assert.match(source,new RegExp(`id=\\"${id}\\"|#${id}`));
  }
  assert.doesNotMatch(source,/>Save view</);
  assert.doesNotMatch(source,/>Run collection</);
  assert.match(source,/incomplete source coverage/);
});
