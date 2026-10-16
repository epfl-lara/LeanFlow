"""Define the trusted, separate-process kernel inspector for formalization receipts."""

INSPECTOR = r"""import Lean
import Lean.Replay
open Lean
-- Rebase only the already checked constants. The kernel record constructor is
-- private, so construct its ordinary record update using compiler reflection.
elab "checkedConstants% " kernel:term ", " constants:term : term => do
  let kernel ← Elab.Term.elabTerm kernel (some (mkConst ``Kernel.Environment))
  let constants ← Elab.Term.elabTerm constants (some (mkConst ``ConstMap))
  let some info := getStructureInfo? (← getEnv) ``Kernel.Environment
    | throwError "pinned compiler lacks kernel environment metadata"
  let fields := info.fieldNames.mapIdx fun idx name =>
    if name == `constants then constants else mkProj ``Kernel.Environment idx kernel
  let constructor := (← getConstInfoInduct ``Kernel.Environment).ctors.head!
  return mkAppN (mkConst constructor) fields
instance : MonadEnv (StateT Environment IO) where
  getEnv := get
  modifyEnv := modify
def kind (info : ConstantInfo) : String :=
  match info with
  | .axiomInfo _ => "axiom"
  | .defnInfo _ => "definition"
  | .thmInfo _ => "theorem"
  | .opaqueInfo _ => "opaque"
  | .quotInfo _ => "quotient"
  | .inductInfo _ => "inductive"
  | .ctorInfo _ => "constructor"
  | .recInfo _ => "recursor"
def sourceRanges? (env : Environment) (name : Name) : Option DeclarationRanges :=
  declRangeExt.find? env name <|> declRangeExt.find? (level := .server) env name
def compiledMatcher (env : Environment) (name : Name) : Bool := Id.run do
  let some idx := env.getModuleIdxFor? name | return false
  return (Meta.Match.Extension.extension.getModuleEntries env idx).any (·.name == name)
def generatedOrigin (env : Environment) (info : ConstantInfo) : List Name × String := Id.run do
  match info with
  | .ctorInfo value => return ([value.induct], "constructor")
  | .recInfo value => return (value.all, "recursor")
  | _ => pure ()
  if let some parent := env.getProjectionStructureName? info.name then
    return ([parent], "projection")
  -- User declarations retain their own compiler source range, including private
  -- declarations. A namespace or internal-looking name never establishes ownership.
  if (sourceRanges? env info.name).isSome then return ([], "")
  if let some (parent, suffix) := Meta.declFromEqLikeName env info.name then
    if Meta.mkEqLikeNameFor env parent suffix == info.name then
      return ([parent], "equation")
  if isAuxRecursor env info.name || isNoConfusion env info.name then
    let mut parents : List Name := []
    for name in info.type.getUsedConstants ++ ((info.value? (allowOpaque := true)).map Expr.getUsedConstants).getD #[] do
      match env.find? name with
      | some (.inductInfo value) =>
        if env.getModuleIdxFor? value.name == env.getModuleIdxFor? info.name then
          parents := value.name :: parents
      | some (.recInfo value) => parents := value.all ++ parents
      | _ => pure ()
    return (parents.eraseDups, if isNoConfusion env info.name then "no_confusion" else "auxiliary_recursor")
  if let .str parent suffix := info.name then
    if let some matcher := privateToUserName? parent then
      if compiledMatcher env matcher && (Meta.isEqnReservedNameSuffix suffix || suffix == "splitter") then
        return ([matcher], "matcher_equation")
    if let some (.ctorInfo ctor) := env.find? parent then
      if suffix ∈ ["inj", "injEq", "sizeOf_spec", "_flat_ctor", "elim"] then
        return ([ctor.induct], "constructor_auxiliary")
    if let some (.inductInfo _) := env.find? parent then
      if suffix ∈ ["noConfusionType", "ctorIdx", "toCtorIdx", "ctorElim", "ctorElimType", "_sizeOf_inst"] ||
          (suffix.startsWith "_sizeOf_" && (suffix.drop 8).toString.toNat?.isSome) then
        return ([parent], "inductive_auxiliary")
    if compiledMatcher env info.name && (sourceRanges? env parent).isSome then
      return ([parent], "matcher")
  -- Elaborator-extracted proof helpers are owned by the source declaration
  -- whose kernel body uses them; names and namespace prefixes are irrelevant.
  if let some idx := env.getModuleIdxFor? info.name then
    let mut parents : List Name := []
    for candidate in env.header.moduleData[idx.toNat]!.constNames do
      if (sourceRanges? env candidate).isNone then continue
      let some parent := env.find? candidate | continue
      let constants := parent.type.getUsedConstants ++
        ((parent.value? (allowOpaque := true)).map Expr.getUsedConstants).getD #[]
      if constants.contains info.name then parents := candidate :: parents
    if !parents.isEmpty then return (parents.eraseDups, "elaborator_dependency")
  return ([], "")
def replayChecked (env : Environment) (directory library compiler : System.FilePath)
    (fullImports : Bool) (arts : NameMap ImportArtifacts) (roles : NameMap String) (roots : Array Name)
    : IO (Environment × Json) := do
  let directory ← IO.FS.realPath directory
  let library ← IO.FS.realPath library
  let compiler ← IO.FS.realPath compiler
  let mut checkedModules : Array Name := #[]
  let mut trustedImports : Array Import := #[]
  let mut imports : Array Json := #[]
  let mut constants : Std.HashMap Name ConstantInfo := {}
  let mut skipped : NameSet := {}
  for moduleName in env.header.moduleNames do
    let path ← IO.FS.realPath (← match arts.find? moduleName >>= ImportArtifacts.olean? with
      | some path => pure path
      | none => findOLean moduleName)
    let compilerModule := (compiler.toString ++ "/").isPrefixOf path.toString
    let localModule := if fullImports then roles.find? moduleName == some "project" else
      (directory.toString ++ "/").isPrefixOf path.toString ||
        (library.toString ++ "/").isPrefixOf path.toString
    if fullImports && !compilerModule && (roles.find? moduleName).isNone then
      throw <| IO.userError s!"import outside recorded mathematical artifact map: {moduleName} ({path})"
    imports := imports.push <| Json.mkObj [
      ("module", toJson moduleName.toString), ("path", toJson path.toString),
      ("role", toJson (if compilerModule then "compiler" else if localModule then "project" else "dependency"))]
    if localModule || (fullImports && !compilerModule) then
      checkedModules := checkedModules.push moduleName
      let some idx := env.getModuleIdx? moduleName | throw <| IO.userError "replay module missing"
      for name in env.header.moduleData[idx.toNat]!.constNames do
        if env.getModuleIdxFor? name != some idx then continue
        let some info := env.find? name | throw <| IO.userError "replay constant missing"
        if fullImports then
          for dependency in info.getUsedConstantsAsSet do
            if dependency == `Lean.reduceBool || dependency == `Lean.reduceNat then
              throw <| IO.userError s!"noncompiler declaration references native reduction: {name} ({dependency})"
        if info.isUnsafe || info.isPartial then
          if localModule then
            throw <| IO.userError s!"unsupported unsafe/partial project declaration: {name}"
          skipped := skipped.insert name
        if let .axiomInfo _ := info then
          throw <| IO.userError s!"unapproved noncompiler axiom: {name}"
        constants := constants.insert name info
    else
      trustedImports := trustedImports.push { module := moduleName }
  let previousPaths ← searchPathRef.get
  let trusted ← try
    if fullImports then searchPathRef.set [compiler]
    let result ← importModules trustedImports {} 0 (leakEnv := true) (loadExts := false)
    if fullImports then
      if !(result.header.moduleNames.contains `Init.Prelude) then
        throw <| IO.userError "trusted compiler base is missing Init.Prelude"
      for moduleName in result.header.moduleNames do
        let path ← IO.FS.realPath (← findOLean moduleName)
        if !(compiler.toString ++ "/").isPrefixOf path.toString then
          throw <| IO.userError s!"trusted module resolves outside pinned compiler: {moduleName}"
    pure result
  finally
    searchPathRef.set previousPaths
  let mut required : NameSet := {}
  let mut pending := if fullImports then roots else constants.toArray.map (·.1)
  let mut cursor := 0
  while cursor < pending.size do
    let name := pending[cursor]!
    cursor := cursor + 1
    if required.contains name then continue
    let some info := constants[name]? | continue
    if skipped.contains name then
      throw <| IO.userError s!"referenced declaration depends on unchecked implementation: {name}"
    required := required.insert name
    pending := pending ++ info.getUsedConstantsAsSet.toArray
    if let .inductInfo info := info then pending := pending ++ info.all.toArray
  let mut replayConstants : Std.HashMap Name ConstantInfo := {}
  let mut replayedModules : NameSet := {}
  for name in required do
    replayConstants := replayConstants.insert name constants[name]!
    if let some idx := env.getModuleIdxFor? name then
      replayedModules := replayedModules.insert env.header.moduleNames[idx.toNat]!
  for (name, info) in constants.toList do
    if let some original := trusted.find? name then
      -- Lean's module system can repeat generated private theorem names. Check
      -- their proof under a fresh name; never silently trust an imported body.
      match original, info with
      | .thmInfo original, .thmInfo duplicate =>
        if original.type != duplicate.type || original.levelParams != duplicate.levelParams then
          throw <| IO.userError s!"imported theorem shadows compiler statement: {name}"
        if required.contains name then
          let mut checkName := name.appendAfter "_leanflow_replayed"
          while trusted.contains checkName || replayConstants.contains checkName do
            checkName := checkName.appendAfter "_"
          replayConstants := replayConstants.erase name |>.insert checkName
            (.thmInfo { duplicate with name := checkName, all := [] })
      | _, _ => throw <| IO.userError s!"imported declaration shadows compiler constant: {name}"
    if required.contains name then
      for dependency in info.getUsedConstantsAsSet do
        if skipped.contains dependency then
          throw <| IO.userError s!"safe declaration {name} depends on unchecked implementation {dependency}"
  let checked ← trusted.replay replayConstants
  -- Replay returns a kernel wrapper whose newly checked constants are in the
  -- staged map. Rebase that checked map for elaborator lookup without copying
  -- any imported extension, matcher, or native-code state from the raw input.
  let kernel := checked.toKernelEnv
  let checkedConstants := kernel.constants.fold
    (fun result name info => result.insert name info) ({} : Std.HashMap Name ConstantInfo)
  let checked := Environment.ofKernelEnv
    (checkedConstants% kernel, (SMap.fromHashMap checkedConstants))
  return (checked, Json.mkObj [
    ("modules", toJson ((replayedModules.toArray.qsort Name.lt).map Name.toString)),
    ("declarations", toJson required.size),
    ("roots", toJson (roots.map Name.toString)),
    ("scanned_modules", toJson (checkedModules.map Name.toString)),
    ("scanned_declarations", toJson constants.size),
    ("axiom_hygiene_scope", toJson (if fullImports then "noncompiler-import-closure" else "project-only")),
    ("skipped_implementation_declarations", toJson ((skipped.toArray.qsort Name.lt).map Name.toString)),
    ("trusted_modules", toJson (trusted.header.moduleNames.map Name.toString)),
    ("imports", toJson imports),
    ("scope", toJson (if fullImports then "referenced-dependency-closure" else "project-only"))])
def inspectionOptions : Options :=
  ({} : Options).setBool `smartUnfolding false |>.setBool `pp.proofs true
    |>.setBool `pp.explicit true |>.setBool `pp.universes true
    |>.setBool `pp.coercions true |>.setBool `pp.coercions.types true
    |>.set `maxHeartbeats (200000 : Nat) |>.set `maxRecDepth (512 : Nat)
def propType (env : Environment) (type : Expr) (name : Name) : IO Bool := do
  try
    -- toIO' starts this bounded query's heartbeat counter after import/replay.
    (Meta.isProp type).run' |>.toIO'
      { fileName := "<formalization-inspector>", fileMap := default,
        options := inspectionOptions } { env }
  catch error => throw <| IO.userError s!"cannot determine proposition for {name}: {error}"
-- Report closed correction facts from replayed expressions, never pretty text.
-- The stricter correction role uses these fields; ordinary draft proofs retain
-- their existing named-obligation policy.
def correctionShape (env : Environment) (info : ConstantInfo) : IO Json := do
  try
    let typeConstantName : Option String :=
      match info.type with
      | .const name [] => some name.toString
      | _ => none
    let typeHeadForall ←
      (do
        let reduced ← Meta.whnf info.type
        pure reduced.isForall).run' |>.toIO'
          { fileName := "<formalization-inspector>", fileMap := default,
            options := inspectionOptions } { env }
    let mut propositionBodyHeadForall : Option Bool := none
    let mut propositionBodyIsProp : Option Bool := none
    match info with
    | .defnInfo definition =>
      match definition.type with
      | .sort .zero =>
        let shape ←
          (do
            let reduced ← Meta.whnf definition.value
            let isProp ← Meta.isProp definition.value
            pure (reduced.isForall, isProp)).run' |>.toIO'
              { fileName := "<formalization-inspector>", fileMap := default,
                options := inspectionOptions } { env }
        propositionBodyHeadForall := some shape.1
        propositionBodyIsProp := some shape.2
      | _ => pure ()
    | _ => pure ()
    return Json.mkObj [
      ("type_constant_name", toJson typeConstantName),
      ("type_head_forall", toJson typeHeadForall),
      ("proposition_body_head_forall", toJson propositionBodyHeadForall),
      ("proposition_body_is_prop", toJson propositionBodyIsProp)]
  catch _ =>
    -- Ordinary declarations do not require this stricter evidence role. A
    -- correction warrant with an unavailable shape still fails closed.
    return Json.mkObj [("inspection_error", toJson "correction_shape_unavailable")]
-- Compute one fixed-point dependency table from checked kernel constants.
-- No imported persistent axiom cache participates, even on newer compilers.
def axiomTable (env : Environment) (roots : Array Name) : IO (NameMap (Array Name)) := do
  let mut reverse : NameMap (Array Name) := {}
  let mut discovered : NameSet := {}
  let mut pending := roots
  let mut seeds : Array (Name × Name) := #[]
  let mut cursor := 0
  while cursor < pending.size do
    let name := pending[cursor]!
    cursor := cursor + 1
    if discovered.contains name then continue
    discovered := discovered.insert name
    let some info := env.find? name
      | throw <| IO.userError s!"checked axiom dependency missing: {name}"
    if let .axiomInfo _ := info then seeds := seeds.push (name, name)
    let dependencies := info.getUsedConstantsAsSet.toArray ++
      (match info with | .inductInfo value => value.ctors.toArray | _ => #[])
    for dependency in dependencies do
      reverse := reverse.insert dependency ((reverse.find? dependency).getD #[] |>.push name)
    pending := pending ++ dependencies
  let mut table : NameMap NameSet := {}
  cursor := 0
  while cursor < seeds.size do
    let (name, axiomName) := seeds[cursor]!
    cursor := cursor + 1
    let known := (table.find? name).getD {}
    if known.contains axiomName then continue
    table := table.insert name (known.insert axiomName)
    for parent in (reverse.find? name).getD #[] do
      seeds := seeds.push (parent, axiomName)
  return table.foldl (fun result name value => result.insert name (value.toArray.qsort Name.lt)) {}
def expressionAxioms (table : NameMap (Array Name)) (expr : Expr) : Array String := Id.run do
  let mut names : NameSet := {}
  for name in expr.getUsedConstants do
    for ax in (table.find? name).getD #[] do names := names.insert ax
  return (names.toArray.qsort Name.lt).map Name.toString
def directSorry (info : ConstantInfo) : Bool :=
  info.value? (allowOpaque := true) |>.any fun expr => expr.getUsedConstants.contains ``sorryAx
def dataSorry (expr : Expr) : Bool := Id.run do
  let mut seen : ExprSet := {}
  let mut pending := #[expr]
  let mut cursor := 0
  while cursor < pending.size do
    let current := pending[cursor]!
    cursor := cursor + 1
    if seen.contains current then continue
    seen := seen.insert current
    match current with
    | .const name levels =>
      if name == ``sorryAx && levels != [.zero] then return true
    | .app fn arg => pending := pending.push fn |>.push arg
    | .lam _ type body _ | .forallE _ type body _ => pending := pending.push type |>.push body
    | .letE _ type value body _ => pending := pending.push type |>.push value |>.push body
    | .mdata _ value | .proj _ _ value => pending := pending.push value
    | _ => pure ()
  return false
def namedProof (info : ConstantInfo) : Bool :=
  kind info == "theorem" && !info.name.isInternal &&
    !info.name.toString.startsWith "_private." && !info.name.toString.contains '#'
def obligationDependencies (env : Environment) (obligations : NameSet)
    (start : Name) (expr : Expr) : Array String × Bool := Id.run do
  let mut pending := expr.getUsedConstants
  let mut seen : NameSet := {}
  let mut result : NameSet := {}
  let mut unknown := false
  let mut cursor := 0
  while cursor < pending.size do
    let name := pending[cursor]!
    cursor := cursor + 1
    if seen.contains name then continue
    seen := seen.insert name
    if obligations.contains name && name != start then
      result := result.insert name
      continue
    if name == ``sorryAx then continue
    let some info := env.find? name | continue
    if directSorry info || info.type.getUsedConstants.contains ``sorryAx then unknown := true
    pending := pending ++ info.type.getUsedConstants
    if let some value := info.value? (allowOpaque := true) then
      pending := pending ++ value.getUsedConstants
  return ((result.toArray.qsort Name.lt).map Name.toString, unknown)
def prettyExpr (env : Environment) (expr : Expr) : IO String := do
  try
    let value ← (Meta.ppExpr expr).run' |>.toIO'
      { fileName := "<formalization-inspector>", fileMap := default,
        options := inspectionOptions } { env }
    pure value.pretty
  catch _ => pure (repr expr).pretty
def writeLibraryEvidence (env metadata : Environment) (names : Array Name)
    (roles : NameMap String) (axiomCache : NameMap (Array Name))
    (reportPath : System.FilePath) : IO Unit := do
  let compilerModules := metadata.header.moduleNames.foldl (fun result name =>
    if (roles.find? name).isNone then result.insert name else result) ({} : NameSet)
  -- The mandatory semantic surface follows statements and data, never proofs.
  -- Keep the larger proof-reachable closure below available for optional audit.
  let mut semanticSurface : NameSet := {}
  let mut surfacePending := names
  let mut surfaceCursor := 0
  while surfaceCursor < surfacePending.size do
    let name := surfacePending[surfaceCursor]!
    surfaceCursor := surfaceCursor + 1
    if semanticSurface.contains name then continue
    semanticSurface := semanticSurface.insert name
    let some idx := metadata.getModuleIdxFor? name | continue
    if compilerModules.contains metadata.header.moduleNames[idx.toNat]! then continue
    let some info := env.find? name | continue
    surfacePending := surfacePending ++ info.type.getUsedConstants
    if !(← propType env info.type name) then
      if let some value := info.value? (allowOpaque := true) then
        surfacePending := surfacePending ++ value.getUsedConstants
  let report ← IO.FS.Handle.mk reportPath IO.FS.Mode.write
  let mut pending := names
  let mut seen : NameSet := {}
  let mut cursor := 0
  while cursor < pending.size do
    let name := pending[cursor]!
    cursor := cursor + 1
    if seen.contains name then continue
    seen := seen.insert name
    let some info := env.find? name | continue
    let some idx := metadata.getModuleIdxFor? name | continue
    let moduleName := metadata.header.moduleNames[idx.toNat]!
    if compilerModules.contains moduleName then continue
    -- Library theorem proofs add no information about the meaning of their
    -- statements. Definitions and opaque constructions remain fully expanded.
    let value := if kind info == "theorem" && !names.contains name then none
      else info.value? (allowOpaque := true)
    let direct := info.type.getUsedConstants ++ (value.map Expr.getUsedConstants).getD #[]
    pending := pending ++ direct
    if roles.find? moduleName == some "project" && names.contains name then continue
    let axioms := (axiomCache.find? name).getD #[]
    report.putStrLn <| Json.compress <| Json.mkObj [
      ("name", toJson name.toString), ("module", toJson moduleName.toString),
      ("role", toJson ((roles.find? moduleName).getD "compiler")),
      ("kind", toJson (kind info)), ("type", toJson (repr info.type).pretty),
      ("pretty_type", toJson (← prettyExpr env info.type)),
      ("pretty_body", toJson (← value.mapM (prettyExpr env))),
      ("levels", toJson (info.levelParams.map Name.toString)),
      ("semantic_surface", toJson (semanticSurface.contains name)),
      ("body", toJson (value.map fun body => (repr body).pretty)),
      ("body_omitted", toJson (kind info == "theorem" && !names.contains name)),
      ("axioms", toJson ((axioms.qsort Name.lt).map Name.toString)),
      ("direct_dependencies", toJson (direct.map Name.toString))]
  report.flush
def declarationRecord (env metadata : Environment) (axiomCache : NameMap (Array Name))
    (obligations : NameSet) (name : Name) : IO Json := do
  let some info := env.find? name | throw <| IO.userError s!"constant missing: {name}"
  let value := info.value? (allowOpaque := true)
  let axioms := (axiomCache.find? name).getD #[]
  let typeAxioms := expressionAxioms axiomCache info.type
  let deps := if axioms.contains ``sorryAx then
      value.map (obligationDependencies env obligations name) |>.getD (#[], false)
    else (#[], false)
  let typeDeps := if typeAxioms.contains "sorryAx" then
      obligationDependencies env obligations name info.type
    else (#[], false)
  let combinedDeps := (deps.1 ++ typeDeps.1).toList.eraseDups
  let direct := info.type.getUsedConstants ++ (value.map Expr.getUsedConstants).getD #[]
  let moduleName := (metadata.getModuleIdxFor? name).map fun idx => metadata.header.moduleNames[idx.toNat]!
  let origin := generatedOrigin metadata info
  let parentProfiles := origin.1.filterMap fun parent => do
    if origin.2 ∉ ["equation", "matcher", "matcher_equation"] then none
    else do
      let info ← env.find? parent
      let idx ← metadata.getModuleIdxFor? parent
      let ranges := sourceRanges? metadata parent
      return Json.mkObj [
        ("name", toJson parent.toString),
        ("module", toJson metadata.header.moduleNames[idx.toNat]!.toString),
        ("kind", toJson (kind info)),
        ("type", toJson (repr info.type).pretty),
        ("levels", toJson (info.levelParams.map Name.toString)),
        ("body", toJson (info.value? (allowOpaque := true) |>.map fun value => (repr value).pretty)),
        ("source_range", toJson (ranges.map fun range => Json.mkObj [
          ("line_start", toJson range.range.pos.line), ("column_start", toJson range.range.pos.column),
          ("line_end", toJson range.range.endPos.line), ("column_end", toJson range.range.endPos.column)]))]
  let ranges := sourceRanges? metadata name
  return Json.mkObj [
    ("name", toJson name.toString),
    ("module", toJson (moduleName.map Name.toString)),
    ("kind", toJson (kind info)),
    ("type", toJson (repr info.type).pretty),
    ("pretty_type", toJson (← prettyExpr env info.type)),
    ("levels", toJson (info.levelParams.map Name.toString)),
    ("body", toJson (value.map fun v => (repr v).pretty)),
    ("is_prop", toJson (← propType env info.type name)),
    ("correction_shape", ← correctionShape env info),
    ("unsafe", toJson info.isUnsafe),
    ("internal", toJson info.name.isInternal),
    ("generated_from", toJson (origin.1.map Name.toString)),
    ("generation_kind", toJson origin.2),
    ("generated_parent_profiles", toJson parentProfiles),
    ("origin_metadata_authority", toJson "untrusted-compiled-navigation-hint"),
    ("line_start", toJson (ranges.map fun r => r.selectionRange.pos.line)),
    ("source_range", toJson (ranges.map fun r => Json.mkObj [
      ("line_start", toJson r.range.pos.line), ("column_start", toJson r.range.pos.column),
      ("line_end", toJson r.range.endPos.line), ("column_end", toJson r.range.endPos.column)])),
    ("type_axioms", toJson typeAxioms),
    ("direct_type_sorry", toJson (info.type.getUsedConstants.contains ``sorryAx)),
    ("type_depends_on", toJson typeDeps.1),
    ("type_untracked_sorry", toJson typeDeps.2),
    ("axioms", toJson ((axioms.qsort Name.lt).map Name.toString)),
    ("direct_sorry", toJson (directSorry info)),
    ("data_sorry", toJson (value.any dataSorry)),
    ("pending", toJson (obligations.contains name)),
    ("depends_on", toJson combinedDeps),
    ("untracked_sorry", toJson (deps.2 || typeDeps.2)),
    ("direct_dependencies", toJson (direct.map Name.toString)),
    ("direct_body_dependencies", toJson ((value.map Expr.getUsedConstants).getD #[] |>.map Name.toString))]
def main (args : List String) : IO Unit := do
  let some directory := args[0]? | throw <| IO.userError "inspection directory required"
  let some library := args[1]? | throw <| IO.userError "project library path required"
  let some compiler := args[2]? | throw <| IO.userError "pinned compiler library path required"
  let some scope := args[3]? | throw <| IO.userError "kernel replay scope required"
  let readiness := scope == "readiness"
  if scope != "closure" && scope != "project" && !readiness then
    throw <| IO.userError "unsupported replay scope"
  let some pathsFile := args[4]? | throw <| IO.userError "recorded library paths required"
  let config ← IO.ofExcept <| Json.parse (← IO.FS.readFile (System.FilePath.mk pathsFile))
  let paths ← IO.ofExcept <| config.getObjValAs? (Array System.FilePath) "paths"
  let arts ← IO.ofExcept <| config.getObjValAs? (NameMap ImportArtifacts) "artifacts"
  let roles ← IO.ofExcept <| config.getObjValAs? (NameMap String) "roles"
  let modules := (args.drop 5).takeWhile (· != "--")
  let requested := ((args.drop 5).dropWhile (· != "--")).drop 1
  if readiness then
    let emptyInputs := Json.mkObj [
      ("paths", Json.arr #[]), ("artifacts", Json.mkObj []), ("roles", Json.mkObj [])]
    if args.drop 5 != ["Init.Prelude", "--"] || config != emptyInputs then
      throw <| IO.userError "readiness requires exactly Init.Prelude, no declarations or artifact inputs"
    if (← IO.FS.realPath (System.FilePath.mk library)) !=
        (← IO.FS.realPath (System.FilePath.mk compiler)) then
      throw <| IO.userError "readiness requires the pinned compiler library only"
  if scope == "closure" || readiness then
    -- Exact recorded artifacts are the only noncompiler import mechanism.
    searchPathRef.set [← IO.FS.realPath (System.FilePath.mk compiler)]
  else
    searchPathRef.modify fun existing => paths.toList ++ existing
  let metadata ← importModules (modules.toArray.map fun m => { module := m.toName }) {} 0
    (leakEnv := true) (loadExts := false) (arts := arts)
  let mut names : Array Name := #[]
  if requested.isEmpty && !readiness then
    for moduleName in modules do
      let some idx := metadata.getModuleIdx? moduleName.toName
        | throw <| IO.userError s!"compiled module missing: {moduleName}"
      for name in metadata.header.moduleData[idx.toNat]!.constNames do
        if metadata.getModuleIdxFor? name == some idx then names := names.push name
  else
    for name in requested do
      if (metadata.find? name.toName).isNone then
        throw <| IO.userError s!"exact declaration missing: {name}"
      names := names.push name.toName
  let (env, replayed) ← replayChecked metadata (System.FilePath.mk directory) (System.FilePath.mk library)
    (System.FilePath.mk compiler) (scope == "closure" || readiness) arts roles names
  IO.FS.writeFile (System.FilePath.mk directory / "kernel-replay.json") (Json.compress replayed)
  let axiomCache ← axiomTable env names
  if readiness then
    IO.FS.writeFile (System.FilePath.mk directory / "library-evidence.jsonl") ""
  else if scope == "closure" then
    writeLibraryEvidence env metadata names roles axiomCache
      (System.FilePath.mk directory / "library-evidence.jsonl")
  let mut obligations : NameSet := {}
  for name in names do
    let some info := env.find? name | continue
    if directSorry info && namedProof info && (← propType env info.type name) then
      obligations := obligations.insert name
  let report ← IO.FS.Handle.mk (System.FilePath.mk directory / "declarations.jsonl") IO.FS.Mode.write
  for name in names do
    report.putStrLn <| Json.compress (← declarationRecord env metadata axiomCache obligations name)
  report.flush
  -- Lazy equations may be emitted in a new module for a definition from an
  -- unchanged imported module. Inspect those parents independently without
  -- promoting them into this draft's authored declarations or obligations.
  let parents ← IO.FS.Handle.mk (System.FilePath.mk directory / "origin-parent-declarations.jsonl") IO.FS.Mode.write
  let mut seen : NameSet := {}
  let mut pending := names
  let mut index := 0
  while index < pending.size do
    let name := pending[index]!
    index := index + 1
    let some info := env.find? name | continue
    let origin := generatedOrigin metadata info
    if origin.2 ∉ ["equation", "matcher", "matcher_equation"] then continue
    for parent in origin.1 do
      if names.contains parent || seen.contains parent then continue
      seen := seen.insert parent
      parents.putStrLn <| Json.compress (← declarationRecord env metadata axiomCache obligations parent)
      pending := pending.push parent
  parents.flush
  if readiness then
    IO.FS.writeFile (System.FilePath.mk directory / "inspector-readiness.json")
      (Json.compress <| Json.mkObj [
        ("readiness_only", toJson true), ("success", toJson true),
        ("proof_acceptance", toJson false),
        ("requested_modules", toJson modules), ("requested_names", toJson requested)])
"""
