export const fixture = {
  meta: { snapshot: '2026-09-16 08:40 UTC', mode: 'demonstration' },
  overview: { unseen: 7, androidUpgrades: 2, securityPatches: 4, sourceWarnings: 1, lastRun: '38 min ago' },
  updates: [
    { id:'u1', maker:'Samsung', device:'Galaxy S25 Ultra', model:'SM-S938B', region:'Israel (ILO)', age:'18 min', buildFrom:'S938BXXU4BYH2', buildTo:'S938BXXU5BYI1', androidFrom:'16', androidTo:'17', patchFrom:'2026-08-01', patchTo:'2026-09-01', change:'Android upgrade', importance:'high', watched:true },
    { id:'u2', maker:'Xiaomi', device:'Xiaomi 15', model:'24129PN74G', region:'Global', age:'2 h', buildFrom:'OS2.0.218.0', buildTo:'OS3.0.4.0', androidFrom:'16', androidTo:'17', patchFrom:'2026-08-01', patchTo:'2026-09-01', change:'Android upgrade', importance:'high', watched:true },
    { id:'u3', maker:'Samsung', device:'Galaxy A56 5G', model:'SM-A566B', region:'Middle East (XSG)', age:'4 h', buildFrom:'A566BXXU3AYH5', buildTo:'A566BXXS4AYI2', androidFrom:'16', androidTo:'16', patchFrom:'2026-08-01', patchTo:'2026-09-01', change:'Security patch', importance:'medium', watched:true },
    { id:'u4', maker:'Google', device:'Pixel 10 Pro', model:'GUL82', region:'Global', age:'5 h', buildFrom:'BP3A.260805.007', buildTo:'BP3A.260905.011', androidFrom:'17', androidTo:'17', patchFrom:'2026-08-05', patchTo:'2026-09-05', change:'Security patch', importance:'medium', watched:false },
    { id:'u5', maker:'Samsung', device:'Galaxy Z Fold7', model:'SM-F966B', region:'Europe (EUX)', age:'8 h', buildFrom:'F966BXXU2AYH4', buildTo:'F966BXXU2AYI6', androidFrom:'16', androidTo:'16', patchFrom:'2026-08-01', patchTo:'2026-09-01', change:'Security patch', importance:'medium', watched:true },
  ],
  devices: [
    { maker:'Samsung', name:'Galaxy S25 Ultra', model:'SM-S938B', chip:'Snapdragon 8 Elite', part:'SM8750-AB', android:17, patch:'2026-09-01', support:'Supported', region:'Global / ILO', confidence:'Verified' },
    { maker:'Samsung', name:'Galaxy A56 5G', model:'SM-A566B', chip:'Exynos 1580', part:'S5E8855', android:16, patch:'2026-09-01', support:'Supported', region:'Global / XSG', confidence:'Verified' },
    { maker:'Xiaomi', name:'Xiaomi 15', model:'24129PN74G', chip:'Snapdragon 8 Elite', part:'SM8750-AB', android:17, patch:'2026-09-01', support:'Supported', region:'Global', confidence:'Verified' },
    { maker:'Xiaomi', name:'POCO F7 Pro', model:'24117RK2CG', chip:'Snapdragon 8 Gen 3', part:'SM8650-AB', android:16, patch:'2026-08-01', support:'Supported', region:'Global', confidence:'Verified' },
    { maker:'Google', name:'Pixel 10 Pro', model:'GUL82', chip:'Google Tensor G5', part:'G5', android:17, patch:'2026-09-05', support:'Supported', region:'Global', confidence:'Verified' },
  ],
  chips: [
    { vendor:'Qualcomm', family:'Snapdragon 8', name:'Snapdragon 8 Elite', part:'SM8750-AB', devices:26, advisories:12, open:3 },
    { vendor:'Qualcomm', family:'Snapdragon 8', name:'Snapdragon 8 Gen 3', part:'SM8650-AB', devices:43, advisories:31, open:7 },
    { vendor:'Samsung', family:'Exynos', name:'Exynos 1580', part:'S5E8855', devices:4, advisories:6, open:2 },
    { vendor:'Google', family:'Tensor', name:'Tensor G5', part:'G5', devices:4, advisories:4, open:1 },
  ],
  security: [
    { cve:'CVE-2026-21473', severity:'Critical', score:'9.8', component:'Qualcomm modem', chip:'SM8650 / SM8750', devices:69, state:'Open', bulletin:'Qualcomm Sep 2026', evidence:'Vendor advisory + component match' },
    { cve:'CVE-2026-30112', severity:'High', score:'8.1', component:'Mali GPU driver', chip:'Tensor G5', devices:4, state:'Claimed fixed', bulletin:'Android Sep 2026', evidence:'SPL 2026-09-05' },
    { cve:'CVE-2026-18840', severity:'High', score:'7.8', component:'Exynos baseband', chip:'S5E8855', devices:4, state:'Unknown', bulletin:'Samsung SVE-2026-1142', evidence:'Fix coordinate unavailable' },
    { cve:'CVE-2026-27551', severity:'Medium', score:'6.4', component:'Android framework', chip:'All Android', devices:185, state:'Claimed fixed', bulletin:'Android Sep 2026', evidence:'SPL 2026-09-01' },
  ],
  health: [
    { source:'Samsung firmware', scope:'Global + Middle East', status:'Healthy', last:'38 min ago', next:'in 5 h', records:'2,418' },
    { source:'Xiaomi firmware', scope:'Global', status:'Healthy', last:'44 min ago', next:'in 5 h', records:'1,207' },
    { source:'Qualcomm bulletins', scope:'Security', status:'Delayed', last:'19 h ago', next:'retry in 22 min', records:'342' },
    { source:'Android bulletins', scope:'Security', status:'Healthy', last:'6 h ago', next:'in 18 h', records:'1,891' },
  ]
};
