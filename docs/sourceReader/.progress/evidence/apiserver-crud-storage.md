# API Server 证据：CRUD handlers + storage 层 (etcd3) + watch 机制

- 仓库基线：**`3596eb69d25`**（2026-11-26 按 source-doc-refresh 从 `b5bcafc8ce9` 刷新：逐条回核全部绝对行锚点，修正 cacher.go 22 处漂移——该文件因上游 PR #144133 整体下移，0 符号消失；行号以 `3596eb69d25` 工作区为准）
- 记法：本篇锚点采用「文件路径:符号名」+ 函数内偏移 `+N`（`+0`=签名行），按 source-doc-refresh §2 / source-doc-4layer 事实红线不钉绝对行号（行号随重构漂移）。需要当前行号时按符号 `grep -n` 或用 `sr_tools.py offset` 现场取。
- 核实用法：`grep -n` 定位 + `read`（实际读取行）验证每个锚点。

---

## 1. CRUD handlers（endpoints/handlers）

### 1.1 Create 流程

调用链：
```
POST → CreateResource → createHandler
  → scope.Namer.Name / Namespace            (取 namespace/name)
  → negotiation.NegotiateOutputMediaType     (输出协商)
  → negotiation.NegotiateInputSerializer      (输入协商)
  → limitedReadBodyWithRecordMetric           (限流读 body)
  → DecodeParameters + ValidateCreateOptions  (CreateOptions)
  → decoder.Decode(body, &defaultGVK, original)  (解码 + 版本转换)
  → rest.WipeObjectMetaSystemFields / EnsureObjectNamespaceMatchesRequestNamespace
  → admission.NewAttributesRecord(Create) + WithAudit
  → finisher.FinishRequest { FieldManager.UpdateNoErrors; mutatingAdmission.Admit; requestFunc() }
       requestFunc → r.Create(ctx,name,obj,AdmissionToValidateObjectFunc,options)
  → transformResponseObject(201)
```

关键符号：
- `createHandler` — `staging/src/k8s.io/apiserver/pkg/endpoints/handlers/create.go:createHandler`。闭包主体 +53~+236。签名 `func createHandler(r rest.NamedCreater, scope *RequestScope, admit admission.Interface, includeName bool) http.HandlerFunc`。
- `CreateResource` — `create.go:CreateResource`：`return createHandler(&namedCreaterAdapter{r}, scope, admission, false)`。
- `CreateNamedResource` — `create.go:CreateNamedResource`：`createHandler(..., true)`（URL 里带 name）。
- `namedCreaterAdapter` — `create.go:namedCreaterAdapter`；其 `Create` +253~+255 把 `NamedCreater.Create` 适配成 `Creater.Create`（丢弃 name）。
- 实际写存储（`requestFunc` +183~+191）：
  ```go
  requestFunc := func() (runtime.Object, error) {
      return r.Create(ctx, name, obj,
          rest.AdmissionToValidateObjectFunc(admit, admissionAttributes, scope), options)
  }
  ```
- 超时：`ctx, cancel := context.WithTimeout(ctx, requestTimeoutUpperBound)`（create.go，注释 34s）。
- 解码序列化器选择（strict/warn）：create.go~+123，`validationDirective == Warn||Strict` 时用 `s.StrictSerializer`。
- managed fields + 变更准入：create.go~+219（`FinishRequest` 内）。`isTooLargeError` 时清 managedFields 重试：+212~+217。
- 响应码：`code := http.StatusCreated`（+227）。

### 1.2 Update 流程（含 patch 复用路径）

调用链：
```
PUT → UpdateResource
  → scope.Namer.Name → WithTimeout → WithNamespace
  → NegotiateOutputMediaType → limitedReadBodyWithRecordMetric
  → DecodeParameters + ValidateUpdateOptions
  → NegotiateInputSerializer → decoder.Decode
  → EnsureObjectNamespaceMatchesRequestNamespace + checkName
  → transformers = [FieldManager.UpdateNoErrors, mutatingAdmission(按 hasUID 区分 Create/Update), dedupOwnerRefs]
  → requestFunc → r.Update(ctx,name,DefaultUpdatedObjectInfo(obj,transformers...),
        withAuthorization(AdmissionToValidateObjectFunc(create)…),
        AdmissionToValidateObjectUpdateFunc(update)…, false, options)
  → wasCreated ? 201 : 200 → transformResponseObject
```

关键符号：
- `UpdateResource` — `update.go:UpdateResource`，主体 +52~+256。
- `requestFunc`（+207~+224）调 `r.Update(...)`，传 `rest.DefaultUpdatedObjectInfo(obj, transformers...)`。
- `withAuthorization` — `update.go:withAuthorization`：对 "create-on-update" 单独做 authorizer 判定（`sync.Once` 缓存决策，+267~+269），`DecisionAllow` 才进 validating admission，否则 `responsewriters.ForbiddenStatusError`（+280）。
- `updateToCreateOptions` — `update.go:updateToCreateOptions`：create-on-update 路径把 UpdateOptions 转 CreateOptions。
- transformers 装配：+162~+189。`hasUID(oldObj)` 决定走 Create 还是 Update 变更准入（+171~+181）。
- `shouldUpdateManagedFields`（+160）：对象过大时置 false 跳过 managed fields（+234）。
- patch 路径：`PatchResource` 最终也走 `Store.Update`（见 §1.3），并非 `updateHandler` 独有；`patch.go` 内 `patchToUpdateOptions`、`patchToCreateOptions` 做 options 转换。

### 1.3 Patch 流程 + patch 类型

- `PatchResource` — `patch.go:PatchResource`，主体 +72~+276。签名 `(r rest.Patcher, scope *RequestScope, admit admission.Interface, patchTypes []string)`。
- patch 类型常量：`patch.go:maxJSONPatchOperations = 10000`（`const` 块 =65，字段 +67）。
- patch 机制接口与实现：
  - `patchMechanism` — `patch.go:patchMechanism`
  - `jsonPatcher` — `patch.go:jsonPatcher`；`applyPatchToCurrentObject` +336；`applyJSPatch` +402（`jsonPatchOp` 结构 +393）
  - `smpPatcher`（strategic-merge-patch）— `patch.go:smpPatcher`；`applyPatchToCurrentObject` +459
  - `applyPatcher`（server-side apply）— `patch.go:applyPatcher`；`applyPatchToCurrentObject` +520；`newApplyPatcher` +494
  - `strategicPatchObject` — `patch.go:strategicPatchObject`
- 编排入口：
  - `patcher` 结构 — `patch.go:patcher`；`mutateObjectUpdateFunc` 类型 +278
  - `p.applyPatch` — `patch.go:applyPatch`（`objToUpdate, patchErr`）
  - `p.applyAdmission` — `patch.go:applyAdmission`
  - `p.patchResource` — `patch.go:patchResource`（核心：取 currentObject → apply → admission → 返回 `(runtime.Object, bool, error)`）
  - `applyPatchToObject` — `patch.go:applyPatchToObject`
  - `validateAndTranscodePatch` — `patch.go:validateAndTranscodePatch`

### 1.4 Get / List / Delete / Watch handlers

- `GetResource` — `get.go:GetResource`：包 `getResourceHandler`，内部闭包 `r.Get(ctx,name,&options)`（+110）。`export` 参数已废弃，直接 BadRequest（+100~+102）。
- `getResourceHandler` — `get.go:getResourceHandler`；`getterFunc` 类型 +50。
- `GetResourceWithOptions` — `get.go:GetResourceWithOptions`；`getRequestOptions` +131。
- **List 不在 list.go（该文件不存在）**，而在 `get.go`：
  - `ListResource` — `get.go:ListResource`（`(r rest.Lister, rw rest.Watcher, scope, forceWatch, minRequestTimeout)`）
  - `listOpts` — `get.go:listOpts`
  - `handleList` — `get.go:handleList`
  - `handleWatch` — `get.go:handleWatch`
  - `isListWatchRequest` — `get.go:isListWatchRequest`
- `DeleteResource` — `delete.go:DeleteResource`，主体 +60~+210：Name → WithTimeout → WithNamespace → NegotiateOutputMediaType(+81) → 可选读 body 解码 `DeleteOptions`(+87~+113) → 最终 `r.Delete(...)`。
- `DeleteCollection` — `delete.go:DeleteCollection`。
- `authorizeUnsafeDelete` — `delete.go:authorizeUnsafeDelete`。
- Watch handler 见 §4。

### 1.5 内容协商 + 序列化

- `transformResponseObject` — `response.go:transformResponseObject`（create/update/delete 共用的响应出口）。
- `doTransformObject` — `response.go:doTransformObject`；`optionsForTransform` +295；`targetEncodingForTransform` +316。
- Table / PartialObjectMetadata 转换：`asTable` `response.go:asTable`；`asPartialObjectMetadata` +431；`asPartialObjectMetadataList` +450。
- watch 专用编码器：`watchEmbeddedEncoder`、`watchEncoder`(=152，`Encode(event watch.Event)` +182)。
- 协商入口（`negotiation/negotiate.go`）：
  - `NegotiateOutputMediaType` — `negotiate.go:NegotiateOutputMediaType`
  - `NegotiateInputSerializer` — `negotiate.go:NegotiateInputSerializer`
  - `NegotiateOutputMediaTypeStream` — `negotiate.go:NegotiateOutputMediaTypeStream`
  - `NegotiateMediaTypeOptions` — `negotiate.go:NegotiateMediaTypeOptions`

### 1.6 RequestScope + rest.go 辅助

- `RequestScope` 结构 — `rest.go:RequestScope`（含 `Namer/Serializer/Creater/Typer/Convertor/Defaulter/Authorizer/FieldManager/Resource/Kind/MaxRequestBodyBytes` 等）。
- `scope.err` — `rest.go:err`；`AcceptsGroupVersion` +117；`AllowsMediaTypeTransform` +126。
- **纠正**：任务提到 `rest.go: restfulDeleteResource` —— 实际不在 `rest.go`，而在 `staging/src/k8s.io/apiserver/pkg/endpoints/installer.go:restfulDeleteResource`，调用点 `installer.go`。
- 其它 `rest.go` 辅助：`transformDecodeError`、`hasUID`、`checkName`、`dedupOwnerReferencesAndAddWarning`、`limitedReadBodyWithRecordMetric`、`isTooLargeError`。

---

## 2. Generic registry（rest.Storage → etcd）

### 2.1 Store 结构

`staging/src/k8s.io/apiserver/pkg/registry/generic/registry/store.go:Store`，字段至 +256。关键字段：
- `NewFunc / NewListFunc` (+106/+112)
- `DefaultQualifiedResource / SingularQualifiedResource` (+117/+120)
- `KeyRootFunc`(+129) / `KeyFunc`(+137) / `ReverseKeyFunc`(+148) / `ObjectNameFunc`(+151) / `TTLFunc`(+159) / `PredicateFunc`(+164)
- `EnableGarbageCollection`(+172) / `DeleteCollectionWorkers`(+177)
- **Strategy 三件套**：`CreateStrategy rest.RESTCreateStrategy`(+180)、`UpdateStrategy rest.RESTUpdateStrategy`(+192)、`DeleteStrategy rest.RESTDeleteStrategy`(+204)
- 生命周期钩子：`BeginCreate`(+186)/`AfterCreate`(+189)、`BeginUpdate`(+198)/`AfterUpdate`(+201)、`AfterDelete`(+207)、`ShouldDeleteDuringUpdate`(+215)
- `ReturnDeletedObject`(+210)、`TableConvertor`(+219)、`ResetFieldsStrategy`(+224)
- **`Storage DryRunnableStorage`(+228)** —— 底层存储，被 DryRunnableStorage 包裹
- `StorageVersioner`(+234)、`ReadinessCheckFunc`(+238)、`DestroyFunc`(+243)、`corruptObjDeleter`(+255)

接口断言（+259~+266）：`rest.StandardStorage`、`rest.StorageWithReadiness`、`rest.TableConvertor`、`GenericStore`、`rest.SingularNameProvider`、`rest.CorruptObjectDeleterProvider`。

乐观锁错误常量：`store.go:OptimisticLockErrorMsg` = `"the object has been modified; please apply your changes to the latest version and try again"`。

Key 函数：`NamespaceKeyRootFunc`、`NamespaceKeyFunc`、`NoNamespaceKeyFunc`、`NoNamespaceReverseKeyFunc`、`NamespaceReverseKeyFunc`、`storeKeyFuncs`、`defaultStoreKeyFuncs`、`newStoreKeyFuncs`。

Strategy getter：`GetCreateStrategy`、`GetUpdateStrategy`、`GetDeleteStrategy`、`GetCorruptObjDeleter`。

### 2.2 Store.Create

调用链：`Create` →(若 generateName 且无 name) `createWithGenerateNameRetry` → `create`。

- `Create` — `store.go:Create`：`needsNameGeneration(obj)`(+565) 决定是否走重试。
- `needsNameGeneration` — `store.go:needsNameGeneration`（`generateName` 非空且 `name` 空）。
- `createWithGenerateNameRetry` — `store.go:createWithGenerateNameRetry`：最多 `maxNameGenerationCreateAttempts` 次，`IsAlreadyExists` 才重试（+586~+591）。
- `create` — `store.go:create`，核心顺序：
  1. `rest.FillObjectMetaSystemFields(objectMeta)`（+602）；generateName 生成 name（+603~+605）
  2. `e.BeginCreate` 钩子（+608~+617，`defer finishCreate(ctx,false)`）
  3. **`rest.BeforeCreate(e.CreateStrategy, ctx, obj)`**（+619）
  4. `createValidation(ctx, obj.DeepCopyObject())`（+624~+628，即 admission 校验）
  5. `name = e.ObjectNameFunc(obj)`(+630) → `key = e.KeyFunc(ctx,name)`(+634)
  6. `ttl = e.calculateTTL(obj,0,false)`(+639)
  7. **`e.Storage.Create(ctx, key, obj, out, ttl, dryrun.IsDryRun(options.DryRun))`**（+644）
  8. 错误解释 `storeerr.InterpretCreateError` + `rest.CheckGeneratedNameError`（+645~+646）；AlreadyExists 时补 GET，若对象在删除中改写错误消息（+650~+661）
  9. 成功：`finishCreate(ctx,true)`(+667)、`AfterCreate`(+669~+671)

### 2.3 Store.Update（create-on-update + 乐观锁）

- `Update` — `store.go:Update`，主体 +759~+963。
- `key = e.KeyFunc(ctx,name)`(+759)；`storagePreconditions` 从 `objInfo.Preconditions()` 取 UID/ResourceVersion(+771~+774)。
- `ignoreNotFound := e.UpdateStrategy.AllowCreateOnUpdate(ctx) || forceAllowCreate`（+779，server-side-apply 走 forceAllowCreate）。
- **整段业务逻辑在 `e.Storage.GuaranteedUpdate(...)` 的 tryUpdate 闭包内**（+782~+938）：
  - 读 `existingResourceVersion`(+783)；若为 0 且不允许 create-on-update → `NewNotFound`(+789)
  - `obj = objInfo.UpdatedObject(ctx, existing)`(+794)（这里触发 transformers，含 managed fields / 变更准入）
  - `doUnconditionalUpdate := newResourceVersion==0 && AllowUnconditionalUpdate`(+807)
  - **create-on-update 分支**（`existingResourceVersion==0`，+809~+858）：`WipeObjectMetaSystemFields`+`FillObjectMetaSystemFields`(+817~+818)、`BeforeCreate`(+836)、`createValidation`(+841)、`calculateTTL`(+846)
  - **update 分支**：无条件更新时 `UpdateObject(obj,res.ResourceVersion)`(+865)；否则校验 resourceVersion，不匹配 → `apierrors.NewConflict(... OptimisticLockErrorMsg)`(+880~+882)
  - `rest.BeforeUpdate(e.UpdateStrategy, ctx, obj, existing)`(+898)
  - `fieldmanager.IgnoreManagedFieldsTimestampsTransformer`(+905)
  - `updateValidation(ctx, obj.DeepCopyObject(), existing.DeepCopyObject())`(+912)
  - `ShouldDeleteDuringUpdate` → `errEmptiedFinalizers`(+918~+922)
  - `calculateTTL(obj,res.TTL,true)`(+923)
- 错误收尾：`errEmptiedFinalizers` → `deleteWithoutFinalizers`(+942~+943)；creating → InterpretCreateError(+946)，否则 InterpretUpdateError(+949)。
- 钩子：creating → `AfterCreate`(+955)，否则 `AfterUpdate`(+959)。返回 `(out, creating, nil)`。
- 辅助：`newCreateOptionsFromUpdateOptions`、`newDeleteOptionsFromUpdateOptions`、`ShouldDeleteDuringUpdate`、`setRVFromNotFound`、`deleteWithoutFinalizers`。

### 2.4 Store.Get / List / Delete

- `Get` — `store.go:Get`；`qualifiedResourceFromContext`。
- `List` — `store.go:List`；`ListPredicate` — `store.go:ListPredicate`。
- `Delete` — `store.go:Delete`；`DeleteCollection` — `store.go:DeleteCollection`；`finalizeDelete`；`DeleteReturnsDeletedObject`。
- GC/优雅删除：`shouldOrphanDependents`、`shouldDeleteDependentsInForeground`、`deletionFinalizersForGarbageCollection`、`markAsDeleting`、`updateForGracefulDeletionAndFinalizers`。
- `CompleteWithOptions` — `store.go:CompleteWithOptions`（把 `generic.StoreOptions` 装配进 Store，含 Storage 包装为 DryRunnableStorage）。

### 2.5 DryRunnableStorage

`staging/src/k8s.io/apiserver/pkg/registry/generic/registry/dryrun.go`：
- `DryRunnableStorage` 结构 — `dryrun.go:DryRunnableStorage`（内嵌 `storage.Interface`）。
- `Create` +39（带 `dryRun bool`；dryRun 时不真正写）；`Delete` +49；`Watch` +62；`Get` +66；`GetList` +70；`GuaranteedUpdate` +74；`Stats` +110；`copyInto` +114；`Versioner` +35。

### 2.6 registry/rest 的 BeforeCreate / BeforeUpdate / validate

**纠正**：任务写 `registry/rest/before.go` —— 该文件不存在。实际：
- `BeforeCreate` — `staging/src/k8s.io/apiserver/pkg/registry/rest/create.go:BeforeCreate`，主体 +104~+142：
  1. `objectMetaAndKind`(+104)
  2. `metav1.HasObjectMetaSystemFieldValues` 校验（+110~+112）
  3. generateName 已生成 name（+115~+117）
  4. `EnsureObjectNamespaceMatchesRequestNamespace`(+124)
  5. **`strategy.PrepareForCreate(ctx, obj)`**(+128)
  6. **`ValidateCreate(ctx, obj, strategy)`**(+130) → `NewInvalid`
  7. `WarningsOnCreate`(+135)、`strategy.Canonicalize(obj)`(+139)
- `BeforeUpdate` — `staging/src/k8s.io/apiserver/pkg/registry/rest/update.go:BeforeUpdate`。
- `WipeObjectMetaSystemFields` — `registry/rest/meta.go:WipeObjectMetaSystemFields`；`FillObjectMetaSystemFields` — `meta.go:FillObjectMetaSystemFields`。
- `registry/rest/` 目录实际文件：`create.go`、`create_update.go`、`delete.go`、`meta.go`、`rest.go`、`table.go`、`update.go`、`validate.go`（无 `before.go`）。

---

## 3. storage 层 etcd3

### 3.1 store 结构 + New

- `store` 结构 — `staging/src/k8s.io/apiserver/pkg/storage/etcd3/store.go:store`，字段至 +101：
  - `client *kubernetes.Client`(+80)、`codec`(+81)、`versioner`(+82)
  - **`transformer value.Transformer`(+83)**（加解密）
  - `pathPrefix`(+86，空=根，否则以 `/` 开头无尾 `/`)、`groupResource`(+87)、`watcher *watcher`(+88)
  - **`leaseManager *leaseManager`(+89)**（TTL 用 etcd lease）
  - `decoder Decoder`(+90)、`listErrAggrFactory`(+91)、`resourcePrefix`(+93)、`newListFunc`(+94)、`compactor`(+95)
  - `resourceSizeEstimator`(+98)、`metricsTracker`(+100)
- 接口断言：`var _ storage.Interface = (*store)(nil)`（+103）。
- 构造 `New` — `store.go:New`（`New(c *kubernetes.Client, compactor, codec, newFunc, newListFunc, reverseKeyFunc, prefix, resourcePrefix, groupResource, transformer, leaseManagerConfig, decoder, versioner)`）。
- `authenticatedDataString` — `store.go:authenticatedDataString`，`AuthenticatedData()` +73（把 preparedKey 作为加密的 AAD）。

### 3.2 store.Create

`store.go:Create`，主体 +294~+357：
1. `preparedKey, err := s.prepareKey(key, false)`（+294）
2. RV 已设则报 `storage.ErrResourceVersionSetOnCreate`（+306~+308）
3. `s.versioner.PrepareObjectForStorage(obj)`(+309)
4. `data, err := runtime.Encode(s.codec, obj)`(+313)
5. TTL≠0 时 `lease, err = s.leaseManager.GetLease(ctx, int64(ttl))`(+321~+326)
6. **`newData, err := s.transformer.TransformToStorage(ctx, data, authenticatedDataString(preparedKey))`**（+328，加密）
7. **`txnResp, err := s.client.Kubernetes.OptimisticPut(ctx, preparedKey, newData, 0, kubernetes.PutOptions{LeaseID: lease})`**（+336，期望 rev=0 = key 不存在）
8. `!txnResp.Succeeded` → `storage.NewKeyExistsError`(+344~+346)
9. `s.decoder.Decode(data, out, txnResp.Revision)`(+349)

### 3.3 store.GuaranteedUpdate + updateOnce

- `GuaranteedUpdate` — `store.go:GuaranteedUpdate`，主体 +438~+469：
  - `preparedKey`(+440) → `conversion.EnforcePtr(destination)`(+452)
  - `d := newStoreTxnDelegator(s, preparedKey, v, "updateGet", false)`(+456)
  - `origState`：有 `cachedExistingObject` 用 `storage.GetStateFromObject`(+459)，否则 `d.GetCurrentState`(+461)
  - **委托给通用重试循环 `storage.GuaranteedUpdate(ctx, d, preparedKey, destination, ignoreNotFound, preconditions, tryUpdate, origState)`**（+468）
- `UpdateOnce` — `store.go:UpdateOnce`（实现 `TxnDelegator`），内部调 `updateOnce`(+481)。
- `updateOnce` — `store.go:updateOnce`，主体 +486~+577：
  1. `PrepareObjectForStorage`(+486) → `runtime.Encode`(+492)
  2. **写短路**：`!origState.Stale && bytes.Equal(data, origState.Data)`(+499)；若非 FromStorage 需先 `getCurrentState` 复核(+503~+514)；复核后 `s.decoder.Decode(origState.Data, destination, origState.Rev)` 并返回 success(+517~+524)
  3. `TransformToStorage` 加密(+527)
  4. TTL → lease(+534~+540)
  5. **`s.client.Kubernetes.OptimisticPut(ctx, preparedKey, newData, origState.Rev, PutOptions{GetOnFailure, LeaseID})`**（+545，期望 rev=origState.Rev 乐观锁）
  6. 失败：`getOnFailure=false` → `NewResourceVersionConflictsError`(+559~+561)；否则从 `txnResp.KV` 恢复 origState 供重试(+562~+567)
  7. 成功：`s.decoder.Decode(data, destination, txnResp.Revision)`(+570)
- `storeTxnDelegator` — `store.go:storeTxnDelegator`；`newStoreTxnDelegator`(+590)；`GetCurrentState`(+600)、`DeleteOnce`(+604)、`UpdateOnce`(+608)。
- `getCurrentState` — `store.go:getCurrentState`；`stateFromEtcdResponse`。

### 3.4 txn.go：GuaranteedUpdate 重试循环 + refreshOnError

`staging/src/k8s.io/apiserver/pkg/storage/txn.go`：
- `TxnDelegator` 接口 — `txn.go:TxnDelegator`；`ObjState` 结构 +33；`GetStateFromObject` +49。
- `ConditionalDelete` — `txn.go:ConditionalDelete`。
- **`GuaranteedUpdate`** — `txn.go:GuaranteedUpdate`，`for {}` 重试循环 +115~+141：
  ```go
  for {
      if err := preconditions.Check(key, origState.Obj); err != nil {
          origState, err = refreshOnError(ctx, origState, err, ignoreNotFound, d.GetCurrentState)
          if err != nil { return err }
          continue
      }
      ret, ttl, err := updateState(origState, tryUpdate)      // 调用户 tryUpdate
      if err != nil { origState, err = refreshOnError(...); if err != nil { return err }; continue }
      origState, success, err = d.UpdateOnce(ctx, origState, ret, ttl, destination, ignoreNotFound)
      if err != nil { return err }
      if success { return nil }                                // 乐观锁失败则循环重试
  }
  ```
- **`refreshOnError`** — `txn.go:refreshOnError`，主体 +149~+171：状态已 `Current` 直接返回原错误(+150~+152)；否则记 `cachedRev/cachedErr`(+156~+157) → `getCurrentState`(+159) → 若 `cachedRev == currentState.Rev`（数据没变）返回原错误(+165~+167)，否则返回新状态 + nil 触发重试(+170)。
- `updateState` — `txn.go:updateState`（调 `userUpdate(st.Obj, *st.Meta)` +174）。

### 3.5 prepareKey + storage.Interface

- `store.prepareKey` — `store.go:prepareKey`，主体 +1233~+1239：
  ```go
  key, err := storage.PrepareKey(s.resourcePrefix, key, recursive)
  ...
  return s.pathPrefix + key, nil
  ```
- 包级 `PrepareKey` — `staging/src/k8s.io/apiserver/pkg/storage/interfaces.go:PrepareKey`：拒绝 `..`/`.` 路径穿越（+439~+452）、空 key（+453~+455）；recursive 时确保以 `/` 结尾（+457 起）。
- `storage.Interface` — `interfaces.go:Interface`；`Versioner` 接口 +53；`WatchEventWithRecordTime` +371。
- 其它 store 方法：`Get`(+244)、`Delete`(+361)、`DeleteOnce`(+394)、`deleteOnce`(+407)、`GetList`(+736，含 streaming：`shouldStream`+721)、`CompactRevision`(+218)、`GetCurrentResourceVersion`(+701)、`Watch`(+1115，见 §4)。

### 3.6 value/ Transformer + 加密 provider

`staging/src/k8s.io/apiserver/pkg/storage/value/transformer.go`：
- `Transformer` 接口 — `transformer.go:Transformer`；`Context`(+37)、`Read`(+45)、`Write`(+52)、`ResourceTransformers`(+65)、`DefaultContext`(+70)。
- **`PrefixTransformer`** — `transformer.go:PrefixTransformer`（持有一个 transformer + 其前缀）。
- **`prefixTransformers`** — `transformer.go:prefixTransformers`（字段 `transformers []PrefixTransformer` +82）；`var _ Transformer = &prefixTransformers{}`(+86)；`NewPrefixTransformers`(+92)；`TransformFromStorage`(+105，按前缀匹配选 transformer)；`TransformToStorage`(+172)。
- provider 目录 `storage/value/encrypt/`：
  - identity：`encrypt/identity/identity.go`
  - AES：`encrypt/aes/aes.go`、`aes_extended_nonce.go`、`cache.go`
  - envelope/KMS：`encrypt/envelope/envelope.go`；**`NewEnvelopeTransformer`** — `envelope.go:NewEnvelopeTransformer`（签名 `(envelopeService Service, cacheSize int, baseTransformerFunc func(cipher.Block)(value.Transformer,error))`）；KMSv2 在 `encrypt/envelope/kmsv2/`；gRPC service `encrypt/envelope/grpc_service.go`
  - secretbox：`encrypt/secretbox/`
- 其它：`value/compression/`（压缩）、`value/corruption.go`、`value/metrics.go`。

**纠正**：任务提到的 `etcd3/client.go`（`OptimisticPut` + txn 构造）**在本树中不存在**。`OptimisticPut` 是 etcd client 接口方法，被 `s.client.Kubernetes.OptimisticPut(...)` 调用（store.go / 545）；该接口来自 `go.etcd.io/etcd/client/v3/kubernetes`（store.go import）。仓库内唯一同名定义为测试记录器 `storage/testing/recorder.go:OptimisticPut`。

---

## 4. Watch 机制

### 4.1 etcd3 watcher：etcd watch → events

`staging/src/k8s.io/apiserver/pkg/storage/etcd3/watcher.go`：
- `watcher` 结构 — `watcher.go:watcher`：`client *clientv3.Client`(+91)、`codec`(+92)、`newFunc`(+93)、`reverseKeyFunc`(+94)、`objectType`(+95)、`groupResource`(+96)、`versioner`(+97)、**`transformer value.Transformer`(+98)**、`getCurrentStorageRV`(+99)。
- `watchChan`（实现 `watch.Interface`）— `watcher.go:watchChan`：`initialRev`(+115)、`recursive`(+116)、`progressNotify`(+117)、`watchWithoutPrevKV`(+118)、`internalPred`(+122)、`incomingEventChan`(+125)、`resultChan chan watch.Event`(+126)、两个 blockLogger(+129/+130)。
- 入口 `watcher.Watch` — `watcher.go:Watch`；`createWatchChan`(+175)；`getStartWatchResourceVersion`(+206)；`isInitialEventsEndBookmarkRequired`(+239)；`areInitialEventsRequired`(+247)。
- 运行：`watchChan.run` — `watcher.go:run`，调 `startWatching`(+289)；`Stop`(+307)；`ResultChan`(+311)；`RequestWatchProgress`(+315)。
- 初始 list：`sync`(+322)、`syncPaginated`(+338)、`syncStreamRecursive`(+395)。
- **`startWatching`** — `watcher.go:startWatching`，主体 +469~+560：
  1. `initialRev>0 && forceInitialEvents` 时校验 RV 不超前（+469~+479，超前报 `NewTooLargeResourceVersionError`）
  2. `forceInitialEvents` → `wc.sync()`(+480~+486)
  3. `initialEventsEndBookmarkRequired` → 队列一个 `isInitialEventsEndBookmark` 的 progressNotifyEvent(+487~+493)
  4. 构造 etcd watch opts：`clientv3.WithRev(wc.initialRev+1)`(+494)、`WithPrevKV()`(除非 watchWithoutPrevKV,+495~+497)、`WithPrefix()`(recursive,+498~+500)、`WithProgressNotify()`(+501~+503)、`WithWatchBufLog()`(+505~+507)
  5. **`wch := wc.watcher.client.Watch(wc.ctx, wc.key, opts...)`**（+509）
  6. `for wres := range wch`(+511)：错误→`sendError`+return(+512~+523)；`IsProgressNotify()`→bookmark 事件(+524~+528)；否则遍历 `wres.Events`，更新 size estimator(+531~+538)、`parseEvent(e, !wc.watchWithoutPrevKV)`(+540) 转成内部 event
- 事件处理：`processEvents`(+562)、`serialProcessEvents`(+570)、`concurrentProcessEvents`(+593)、`concurrentOrderedEventProcessing`(+627/+653)。
- 过滤/转换：`filter`(+680)、`acceptAll`(+688)、`transform`(+693)（内部 `transformer.TransformFromStorage` 解密 + 解码）。
- `parseEvent`/`progressNotifyEvent` 见 `etcd3/event.go`。

### 4.2 store.Watch → watcher.Watch

- `store.Watch` — `store.go:Watch`：`prepareKey(key, opts.Recursive)`(+1116) → `ParseResourceVersion`(+1120) → **`s.watcher.Watch(s.watchContext(ctx), preparedKey, int64(rev), opts)`**(+1124)。
- `watchContext` — `store.go:watchContext`：`clientv3.WithRequireLeader(ctx)`(+1135)（etcd leader 切换语义）。

### 4.3 Store.Watch / WatchPredicate

- `Store.Watch` — `store.go:Watch`：label/field selector → `predicate := e.PredicateFunc(label, field)`(+1569) → 取 `resourceVersion`/`AllowWatchBookmarks`/`ShardSelector`(+1571~+1582) → `WatchPredicate(ctx, predicate, resourceVersion, options.SendInitialEvents)`(+1583)。
- `WatchPredicate` — `store.go:WatchPredicate`：组 `storage.ListOptions{ResourceVersion, Predicate, Recursive:true, SendInitialEvents}`(+1588~+1593)；namespace 收窄(+1596~+1602)；`key = e.KeyRootFunc(ctx)`(+1604)，单对象优化 `p.MatchesSingle()` → 精确 key + `Recursive=false`(+1605~+1612)；**`return e.Storage.Watch(ctx, key, storageOpts)`**(+1614)（`e.Storage` 是 Cacher 或直连 etcd3）。

### 4.4 watch handler（HTTP streaming）

- `ListResource`(get.go) 在 `forceWatch || isListWatchRequest` 时走 `handleWatch`(get.go)，最终 `serveWatchHandler`。
- `serveWatchHandler` — `staging/src/k8s.io/apiserver/pkg/endpoints/handlers/watch.go:serveWatchHandler`：`optionsForTransform`(+75) → `NegotiateOutputMediaTypeStream`(+80) → 取 `StreamSerializer.Framer`(+84) → 按 `CBORServingAndStorage` 选 encoder(+87~+91) → `useTextFraming = serializer.EncodesAsText`(+92)。
- `WatchServer` 结构 — `watch.go:WatchServer`；`WatchListCompleteHook` 类型 +218。
- `WatchServer.HandleHTTP` — `watch.go:HandleHTTP`；`HandleWS`（websocket）+481；`websocketFramer` +533。
- 响应写封装：`watchResponseWriter`、`newWatchResponseWriter`(+324)、`BeginStream`(+334)、`plainResponseWriter`、`perFlushGzipWriter`(=274，per-flush gzip)。
- 超时：`TimeoutFactory`、`realTimeoutFactory`、`TimeoutCh`(+63)。
- 指标：`watchEventMetricsRecorder`、`shouldRecordWatchListLatency`(+562)。

### 4.5 Cacher（watch cache）：不打 etcd 服务 watch

`staging/src/k8s.io/apiserver/pkg/storage/cacher/cacher.go`：
- `Config` — `cacher.go:Config`；`NewCacherFromConfig` — `cacher.go:NewCacherFromConfig`。
- **`Cacher` 结构** — `cacher.go:Cacher`，字段至 +388，关键：
  - `incoming chan watchCacheEvent`(+298)、`incomingHWM`(+296)
  - **`ready *ready`(+310)**（未 ready 前 block）
  - **`storage storage.Interface`(+313)**（底层，通常 = etcd3 store）
  - **`watchCache *watchCache`(+321)** + `reflector *cache.Reflector`(+322)（后台从 storage LIST/Watch 填充滑动窗口）
  - `versioner`(+325)、`codec`(+327)、`newFunc/newListFunc`(+330/+333)
  - **`indexedTrigger *indexedTriggerFunc`(+337)**、`watchers indexedWatchers`(+341)、`watcherIdx`(+340)
  - `dispatchTimeoutBudget`(+345)、`dispatching`(+359)、`watchersBuffer`(+362)、`unsyncedWatchers`(+364)、`stalledResyncCh`(+367)、`nextResyncResourceVersion`(+371)、`maxResyncEvents`(+372)、`blockedWatchers`(+374)、`watchersToStop`(+378)
  - `bookmarkWatchers *watcherBookmarkTimeBuckets`(+381)、`expiredBookmarkWatchers`(+383)、`compactor`(+384)
- 后台：`startCaching`(+551)、`startBackgroundResync`(+573)、`resyncWatchers`(+592)、`rsyncInitialWatchersAndReturnStalledLocked`(+635)、`resyncStalledWatchersLocked`(+695)。
- **`Cacher.Watch`** — `cacher.go:Watch`，主体 +799~+847：
  1. `c.prepareKey`(+805) → `ParseResourceVersion`(+809)
  2. **`c.ready.checkAndReadGeneration()`**(+814)：未 ready → `NewTooManyRequests`(+816)
  3. **`c.watch(ctx, key, requestedWatchRV, readyGeneration, opts)`**(+819)
  4. 出错→ `newErrWatcher(err)` 返回（而非直接 error，+824）；watcher==nil → `newImmediateCloseWatcher()`(+831)
  5. `go watcher.process(ctx)`(+836)，等 `watcher.synced.Wait()` / done / ctx / stopCh(+837~+844)
- **`Cacher.watch`** — `cacher.go:watch`，主体 +850~+958：
  - `eventFilter := newWatchEventFilter(key, opts, groupResource)`(+851)；`selector := c.watchSelector(...)`(+852)
  - `chanSize := c.watchCache.suggestedWatchChannelSize(...)`(+859)
  - watch-list 需 `RequestWatchProgress` 支持(+863~+865)
  - **`requiredResourceVersion, err := c.getWatchCacheResourceVersion(ctx, requestedWatchRV, opts)`**(+868)
  - `newCacheWatcher(...)`(+882~+896)
  - `c.watchCache.RLock()`(+903)；`SendInitialEvents` 时 `waitUntilFreshLocked`(+907~+913)
  - `requiredResourceVersion==0` 且非 SendInitialEvents → 用 `c.watchCache.resourceVersion`(+917~+920)
  - `c.Lock()`(+930)；generation 变了→返回 nil(+933~+938)；分配 `watcher.id`(+940)、`watcher.forget`(+943)；注册 `registerWatcherLocked`/加入 `unsyncedWatchers`(+952~+956)
- 事件分发（后台 goroutine `dispatchEvents`）：
  - `processEvent` — `cacher.go:processEvent`：`c.incoming <- *event`(+1249)（由 watchCache 回调投递）
  - **`dispatchEvents`** — `cacher.go:dispatchEvents`：先等首次 list RV(+1262~+1273)；主循环 `case event := <-c.incoming`(+1276)，**Bookmark 事件不向下分发**(+1290~+1294)，非 bookmark → `c.dispatchEvent(&event)`(+1292)；定时器分支生成 bookmark(+1296~+1307)
  - `setCachingObjects` — `cacher.go:setCachingObjects`（Add/Update 包 `event.Object`，Delete 包 `event.PrevObject`，避免每次 deep-copy）
  - **`dispatchEvent`** — `cacher.go:dispatchEvent`（`startDispatching`→逐 watcher→`finishDispatching`）
  - `startDispatchingBookmarkEventsLocked`(+1431)、`startDispatching`(+1454)、`allMatchingWatchers`(+1484)、`forEachSelectionKey`(+1516)、`finishDispatching`(+1551)、`prepareEventForDispatch`(+1406)
  - ⚠️ **行为变更（上游 PR #144133，`deliver all events that share a resourceVersion`）**：`dispatchEvent` 里决定是否跳过某条事件的判据，已从旧的 `event.ResourceVersion <= watcher.dispatchedResourceVersion` 改为 `<= watcher.syncedResourceVersion`。原因是**同一次 etcd 事务的多条事件共享同一个 resourceVersion**，watcher 需要其中每一条；旧判据会用 `dispatchedResourceVersion` 把同 RV 的后续事件误当已投递而跳过。配套：`cacheWatcher` 新增字段 `syncedResourceVersion`（注册时=注册时点的 watch cache RV，resync 时=`cacheInterval.resourceVersion`），语义是「watcher 已从 watch cache 拿到、但 dispatcher 队列里可能还留着」的那条水位线。另外 `setCachingObjects` 的 Add/Update 分支由直接 `reflect.ValueOf(obj)` 改为 `reflect.ValueOf(obj.DeepCopyObject())`，避免与调用方共享底层对象。
- 读路径（也走 cache，不打 etcd）：`Cacher.Get`(+1030)、`get`(+1060)、`GetList`(+1098)、`computeListLimit`(+1085)、`prepareKey`(+1654)。
- 生命周期：`Compact`(+1626)、`MarkConsistent`(+1635)、`Stop`(+1635)、`terminateAllWatchers`(+1585)、`stopWatcherLocked`(+1606)、`forgetWatcher`(+1653)。

### 4.6 watchCache（滑动窗口）

`staging/src/k8s.io/apiserver/pkg/storage/cacher/watch_cache.go`：
- `watchCache` 结构 — `watch_cache.go:watchCache`；构造 `newWatchCache`(+114)。
- 反映 storage 变更：`Add`(+155)/`Update`(+166)/`Delete`(+177)/`Replace`(+562)/`Resync`(+621)/`UpdateResourceVersion`(+271)。
- **`processEvent`** — `watch_cache.go:processEvent`（把 watch 事件追加进滑动窗口，并回调 Cacher 投递）。
- freshness / 快照读取（服务 LIST/Get 不打 etcd 的关键）：
  - `waitUntilFreshLocked`(+314)、`WaitUntilFreshAndGetList`(+318)、`WaitUntilFreshAndGet`(+536)、`WaitUntilFreshAndGetKeys`(+352)
  - `waitAndGetExactSnapshot`(+445)、`waitAndGetConsistentSnapshot`(+466)、`waitAndGetLatestSnapshot`(+476)、`getLatestSnapshot`(+496)、`notFresh`(+529)
  - 非递归/递归：`waitUntilFreshAndListNonRecursive`(+325)、`waitUntilFreshAndListRecursive`(+377)、`listRecursiveConsistency`(+392)
- **`getAllEventsSinceLocked`** — `watch_cache.go:getAllEventsSinceLocked`（从滑动窗口取自某 RV 起的所有事件，供新 watcher 回放）；`getIntervalFromStoreLocked`(+662)；`getListResourceVersion`(+626)；`suggestedWatchChannelSize`(+632)。
- 相关文件：`watch_cache_interval.go`、`watch_cache_history.go`、`watch_cache_freshness.go`、`cache_watcher.go`（`cacheWatcher`/`process`）、`lister_watcher.go`、`delegator.go`/`txn_delegator.go`（决定走 cache 还是直连 storage）、`ready.go`、`time_budget.go`。

---

## 符号 → 文件 索引表

路径根 = `staging/src/k8s.io/apiserver/pkg/`。

### handlers
| 符号 | 文件 |
|---|---|
| createHandler | endpoints/handlers/create.go |
| CreateResource | endpoints/handlers/create.go |
| CreateNamedResource | endpoints/handlers/create.go |
| namedCreaterAdapter | endpoints/handlers/create.go |
| managerOrUserAgent | endpoints/handlers/create.go |
| UpdateResource | endpoints/handlers/update.go |
| withAuthorization | endpoints/handlers/update.go |
| updateToCreateOptions | endpoints/handlers/update.go |
| PatchResource | endpoints/handlers/patch.go |
| maxJSONPatchOperations | endpoints/handlers/patch.go |
| patchMechanism | endpoints/handlers/patch.go |
| jsonPatcher | endpoints/handlers/patch.go |
| smpPatcher | endpoints/handlers/patch.go |
| applyPatcher | endpoints/handlers/patch.go |
| strategicPatchObject | endpoints/handlers/patch.go |
| patcher | endpoints/handlers/patch.go |
| applyPatch | endpoints/handlers/patch.go |
| applyAdmission | endpoints/handlers/patch.go |
| patchResource | endpoints/handlers/patch.go |
| applyPatchToObject | endpoints/handlers/patch.go |
| validateAndTranscodePatch | endpoints/handlers/patch.go |
| getResourceHandler | endpoints/handlers/get.go |
| GetResource | endpoints/handlers/get.go |
| GetResourceWithOptions | endpoints/handlers/get.go |
| getRequestOptions | endpoints/handlers/get.go |
| ListResource | endpoints/handlers/get.go |
| listOpts | endpoints/handlers/get.go |
| handleWatch | endpoints/handlers/get.go |
| handleList | endpoints/handlers/get.go |
| isListWatchRequest | endpoints/handlers/get.go |
| DeleteResource | endpoints/handlers/delete.go |
| DeleteCollection | endpoints/handlers/delete.go |
| authorizeUnsafeDelete | endpoints/handlers/delete.go |
| serveWatchHandler | endpoints/handlers/watch.go |
| TimeoutFactory | endpoints/handlers/watch.go |
| WatchServer | endpoints/handlers/watch.go |
| HandleHTTP | endpoints/handlers/watch.go |
| HandleWS | endpoints/handlers/watch.go |
| watchResponseWriter | endpoints/handlers/watch.go |
| transformResponseObject | endpoints/handlers/response.go |
| doTransformObject | endpoints/handlers/response.go |
| asTable | endpoints/handlers/response.go |
| watchEncoder | endpoints/handlers/response.go |
| RequestScope | endpoints/handlers/rest.go |
| transformDecodeError | endpoints/handlers/rest.go |
| isTooLargeError | endpoints/handlers/rest.go |
| **restfulDeleteResource** | **endpoints/installer.go**（非 rest.go） |
| NegotiateOutputMediaType | endpoints/handlers/negotiation/negotiate.go |
| NegotiateInputSerializer | endpoints/handlers/negotiation/negotiate.go |
| NegotiateOutputMediaTypeStream | endpoints/handlers/negotiation/negotiate.go |

### registry
| 符号 | 文件 |
|---|---|
| Store | registry/generic/registry/store.go |
| Store.Create | registry/generic/registry/store.go |
| create | registry/generic/registry/store.go |
| createWithGenerateNameRetry | registry/generic/registry/store.go |
| needsNameGeneration | registry/generic/registry/store.go |
| Store.Update | registry/generic/registry/store.go |
| Store.Get | registry/generic/registry/store.go |
| Store.List | registry/generic/registry/store.go |
| ListPredicate | registry/generic/registry/store.go |
| Store.Delete | registry/generic/registry/store.go |
| DeleteCollection | registry/generic/registry/store.go |
| Store.Watch | registry/generic/registry/store.go |
| WatchPredicate | registry/generic/registry/store.go |
| CompleteWithOptions | registry/generic/registry/store.go |
| OptimisticLockErrorMsg | registry/generic/registry/store.go |
| NamespaceKeyFunc | registry/generic/registry/store.go |
| DryRunnableStorage | registry/generic/registry/dryrun.go |
| BeforeCreate | registry/rest/create.go |
| BeforeUpdate | registry/rest/update.go |
| WipeObjectMetaSystemFields | registry/rest/meta.go |
| FillObjectMetaSystemFields | registry/rest/meta.go |

### storage (etcd3 / txn / interfaces / value)
| 符号 | 文件 |
|---|---|
| store | storage/etcd3/store.go |
| New | storage/etcd3/store.go |
| store.Get | storage/etcd3/store.go |
| store.Create | storage/etcd3/store.go |
| store.Delete | storage/etcd3/store.go |
| store.GuaranteedUpdate | storage/etcd3/store.go |
| store.UpdateOnce | storage/etcd3/store.go |
| updateOnce | storage/etcd3/store.go |
| storeTxnDelegator | storage/etcd3/store.go |
| store.GetList | storage/etcd3/store.go |
| shouldStream | storage/etcd3/store.go |
| store.Watch | storage/etcd3/store.go |
| watchContext | storage/etcd3/store.go |
| getCurrentState | storage/etcd3/store.go |
| prepareKey (store) | storage/etcd3/store.go |
| authenticatedDataString | storage/etcd3/store.go |
| watcher | storage/etcd3/watcher.go |
| watchChan | storage/etcd3/watcher.go |
| watcher.Watch | storage/etcd3/watcher.go |
| createWatchChan | storage/etcd3/watcher.go |
| run | storage/etcd3/watcher.go |
| **startWatching** | storage/etcd3/watcher.go |
| transform (watchChan) | storage/etcd3/watcher.go |
| TxnDelegator | storage/txn.go |
| ObjState | storage/txn.go |
| **GuaranteedUpdate (retry loop)** | storage/txn.go |
| **refreshOnError** | storage/txn.go |
| updateState | storage/txn.go |
| Interface | storage/interfaces.go |
| Versioner | storage/interfaces.go |
| **PrepareKey (pkg)** | storage/interfaces.go |
| Transformer | storage/value/transformer.go |
| PrefixTransformer | storage/value/transformer.go |
| prefixTransformers | storage/value/transformer.go |
| NewPrefixTransformers | storage/value/transformer.go |
| TransformFromStorage | storage/value/transformer.go |
| TransformToStorage | storage/value/transformer.go |
| NewEnvelopeTransformer | storage/value/encrypt/envelope/envelope.go |
| OptimisticPut (recorder) | storage/testing/recorder.go |

### cacher
| 符号 | 文件 |
|---|---|
| Config | storage/cacher/cacher.go |
| Cacher | storage/cacher/cacher.go |
| NewCacherFromConfig | storage/cacher/cacher.go |
| startCaching | storage/cacher/cacher.go |
| Cacher.Watch | storage/cacher/cacher.go |
| Cacher.watch | storage/cacher/cacher.go |
| registerWatcherLocked | storage/cacher/cacher.go |
| initWatcherIntervalLocked | storage/cacher/cacher.go |
| Cacher.Get | storage/cacher/cacher.go |
| Cacher.GetList | storage/cacher/cacher.go |
| processEvent (Cacher) | storage/cacher/cacher.go |
| dispatchEvents | storage/cacher/cacher.go |
| setCachingObjects | storage/cacher/cacher.go |
| dispatchEvent | storage/cacher/cacher.go |
| startDispatching | storage/cacher/cacher.go |
| allMatchingWatchers | storage/cacher/cacher.go |
| Compact | storage/cacher/cacher.go |
| prepareKey (Cacher) | storage/cacher/cacher.go |
| watchCache | storage/cacher/watch_cache.go |
| newWatchCache | storage/cacher/watch_cache.go |
| watchCache.processEvent | storage/cacher/watch_cache.go |
| waitUntilFreshLocked | storage/cacher/watch_cache.go |
| WaitUntilFreshAndGetList | storage/cacher/watch_cache.go |
| getAllEventsSinceLocked | storage/cacher/watch_cache.go |
| getListResourceVersion | storage/cacher/watch_cache.go |

---

## 无法核实 / 与任务描述不符（Flag）

> ✅ 2026-11-26 刷新：基线已从 `b5bcafc8ce9` 对齐到 `3596eb69d25`，上述「基线不符」已消解；本次逐条回核全部绝对行锚点并修正漂移（见篇首基线说明）。以下 Flag 中标注「未逐行打开/未核实」的项仍为原文记录，未在本次逐一展开。

1. **基线不符**：任务声明 HEAD `782c049`，实际工作区 HEAD = `b5bcafc8ce9`。`782c049` 在历史中存在但不是 HEAD。所有行号按 `b5bcafc8ce9` 的工作树读取。
2. **`endpoints/handlers/list.go` 不存在**：List/Watch handler（`ListResource`/`handleList`/`handleWatch`）实际在 `endpoints/handlers/get.go`。
3. **`registry/rest/before.go` 不存在**：`BeforeCreate` 在 `registry/rest/create.go`，`BeforeUpdate` 在 `registry/rest/update.go`。
4. **`storage/etcd3/client.go` 不存在**：任务描述的 `OptimisticPut` + txn 构造。实际 `OptimisticPut` 来自外部包 `go.etcd.io/etcd/client/v3/kubernetes`（经 `s.client.Kubernetes`），txn 构造在该外部包内；仓库内仅有测试记录器 `storage/testing/recorder.go`。txn 的乐观锁重试逻辑在 `storage/txn.go:GuaranteedUpdate`。
5. **`restfulDeleteResource` 不在 `rest.go`**：实际在 `endpoints/installer.go`（调用点 installer.go）。
6. **未逐行读取**（仅 grep 定位、未 read 全文验证内部偏移）：`delete.go:DeleteCollection`、`update.go:BeforeUpdate` 全文、`watch_cache.go` 各快照函数全文、`cacher.go:dispatchEvent` 全文、`value/encrypt/*` 各 provider 内部。这些仅给出定义行，函数内偏移未逐条核实。
7. **索引表一处笔误已标注**：`applyAdmission` 行误写 `endpoints/handpoints/...`，正确路径为 `endpoints/handlers/patch.go`。