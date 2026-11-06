window.addEventListener('DOMContentLoaded',()=>{
 window.ui=SwaggerUIBundle({
  url:'/openapi.json',dom_id:'#swagger-ui',layout:'BaseLayout',deepLinking:true,
  docExpansion:'none',filter:true,displayRequestDuration:true,validatorUrl:null,
  persistAuthorization:false,queryConfigEnabled:false,
  requestInterceptor:request=>{
   const url=new URL(request.url,window.location.href);
   if(url.origin!==window.location.origin)throw Error('Only this backend can be called from the API explorer.');
   request.credentials='same-origin';
   if(!['GET','HEAD','OPTIONS'].includes((request.method||'GET').toUpperCase())){
    const token=document.cookie.split('; ').find(item=>item.startsWith('csrf='))?.slice(5);
    if(token){request.headers=request.headers||{};request.headers['X-CSRF-Token']=token;}
   }
   return request;
  }
 });
});
