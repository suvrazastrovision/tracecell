const assert=require('assert'),fs=require('fs'),vm=require('vm');
let ready,config;
const context={window:{location:{href:'http://127.0.0.1:8767/docs',origin:'http://127.0.0.1:8767'},addEventListener:(_,fn)=>{ready=fn;}},
 document:{cookie:'csrf=unit-test-token'},URL,SwaggerUIBundle:options=>{config=options;return {};}};
vm.runInNewContext(fs.readFileSync('src/pbmc_platform/app/static/api-docs.js','utf8'),context);
ready();assert.equal(config.url,'/openapi.json');assert.equal(config.validatorUrl,null);
const write=config.requestInterceptor({url:'/api/uploads',method:'POST'});
assert.equal(write.headers['X-CSRF-Token'],'unit-test-token');assert.equal(write.credentials,'same-origin');
const read=config.requestInterceptor({url:'/api/runs',method:'GET'});assert.equal(read.headers,undefined);
assert.throws(()=>config.requestInterceptor({url:'https://other.example/api',method:'POST'}),/Only this backend/);
console.log('PASS: Swagger loads the local schema, disables remote validation and adds CSRF only to same-origin writes.');
