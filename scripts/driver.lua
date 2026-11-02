-- poe1-builds driver: runs inside Path of Building Community (headless) to
-- build, validate and calculate Path of Exile 1 builds.
--
-- Usage (cwd must be PoB's src/ directory):
--   luajit driver.lua <request.json> <response.json>
-- The request has a "mode" field: build | check | nodes | masteries | gems |
-- uniques | bases | mods | classes | config

local REQ_PATH, RESP_PATH = arg[1], arg[2]

---------------------------------------------------------------------------
-- PoB files start with "#@" which plain LuaJIT can't parse; strip it.
---------------------------------------------------------------------------
local _loadfile = loadfile
function loadfile(name, ...)
	local f = io.open(name, "rb")
	if not f then return _loadfile(name, ...) end
	local s = f:read("*a")
	f:close()
	if s:sub(1, 1) == "#" then s = "--" .. s end
	return loadstring(s, "@" .. name)
end
function dofile(name)
	local fn = assert(loadfile(name))
	return fn()
end

-- Silence PoB's console chatter so stdout stays readable.
local realPrint = print
local quiet = os.getenv("POB_VERBOSE") ~= "1"
if quiet then
	print = function() end
end

dofile("HeadlessWrapper.lua")
local json = require("dkjson")

---------------------------------------------------------------------------
-- Small helpers
---------------------------------------------------------------------------
local t_insert, t_concat = table.insert, table.concat

local function readFile(p)
	local f = assert(io.open(p, "rb"))
	local s = f:read("*a")
	f:close()
	return s
end
local function writeFile(p, s)
	local f = assert(io.open(p, "wb"))
	f:write(s)
	f:close()
end
local function stripColor(s)
	if type(s) ~= "string" then return s end
	s = s:gsub("%^x%x%x%x%x%x%x", ""):gsub("%^%d", "")
	return s
end
local function lc(s) return s and tostring(s):lower() or "" end
local function norm(s) return (lc(s):gsub("[^%w]", "")) end
local function trim(s) return (tostring(s or ""):gsub("^%s+", ""):gsub("%s+$", "")) end
local function round(v, d)
	if type(v) ~= "number" then return v end
	local m = 10 ^ (d or 1)
	return math.floor(v * m + 0.5) / m
end
local function contains(hay, needle) return lc(hay):find(lc(needle), 1, true) ~= nil end

local resp = { errors = {}, warnings = {}, notes = {} }
local function err(msg) t_insert(resp.errors, msg) end
local function warn(msg) t_insert(resp.warnings, msg) end
local function note(msg) t_insert(resp.notes, msg) end

-- In batch mode a fatal error must end only the current request, not the process.
local BATCH = false
local FINISH = {}
local function finish()
	if BATCH then error(FINISH, 0) end
	resp.valid = #resp.errors == 0
	writeFile(RESP_PATH, json.encode(resp, { indent = true }))
	os.exit(0)
end

-- Optional stage timing (request flag "timing"): CPU seconds per stage, no data content.
local TIMING = false
local stageStart = os.clock()
local function stage(name)
	if not TIMING then return end
	local now = os.clock()
	resp.timing = resp.timing or {}
	t_insert(resp.timing, { name, math.floor((now - stageStart) * 1000 + 0.5) / 1000 })
	stageStart = now
end

local function frame()
	runCallback("OnFrame")
end

-- Quest passive points by act (mirrors PoB's Build.lua table).
local acts = {
	{ level = 1, questPoints = 0 }, { level = 12, questPoints = 2 }, { level = 22, questPoints = 4 },
	{ level = 32, questPoints = 6 }, { level = 40, questPoints = 7 }, { level = 44, questPoints = 9 },
	{ level = 50, questPoints = 12 }, { level = 54, questPoints = 15 }, { level = 60, questPoints = 18 },
	{ level = 64, questPoints = 20 }, { level = 67, questPoints = 23 },
}
local function pointsAvailable(level, extra)
	local act = 1
	for i, a in ipairs(acts) do
		if level >= a.level then act = i end
	end
	return (level - 1) + acts[act].questPoints + ((act > 2) and (extra or 0) or 0)
end
-- Labyrinth ascendancy points expected by level (normal/cruel/merciless/uber).
local function ascPointsAvailable(level)
	if level >= 75 then return 8 elseif level >= 68 then return 6 elseif level >= 55 then return 4 elseif level >= 33 then return 2 end
	return 0
end

---------------------------------------------------------------------------
-- Lookups
---------------------------------------------------------------------------
local function treeObj() return build.spec.tree end

local function findClass(name)
	for id, class in pairs(treeObj().classes) do
		if norm(class.name) == norm(name) then return id, class end
	end
end
local function findAscendancy(class, name)
	if not name or name == "" or lc(name) == "none" then return 0 end
	for id, asc in pairs(class.classes) do
		if id ~= 0 and norm(asc.name) == norm(name) then return id, asc end
	end
	-- maybe the user picked an ascendancy of another class
	for _, other in pairs(treeObj().classes) do
		for id, asc in pairs(other.classes) do
			if id ~= 0 and norm(asc.name) == norm(name) then
				return nil, nil, other.name
			end
		end
	end
end
local function findBloodline(name)
	local alt = treeObj().alternate_ascendancies
	if not alt or not name then return nil end
	for id, asc in pairs(alt) do
		if norm(asc.name) == norm(name) then return id, asc end
	end
end

local function nodeStats(node)
	local out = {}
	for _, line in ipairs(node.sd or {}) do t_insert(out, stripColor(line)) end
	return out
end

-- Real, allocatable tree nodes only (skip cluster proxies, anoint-only nodes...)
local function isRealNode(node)
	if not node or node.isProxy or node.isBlighted then return false end
	if node.type == "ClassStart" or node.type == "AscendClassStart" then return false end
	if not node.group and not node.ascendancyName then return false end
	if node.expansionJewel and not node.group then return false end
	return true
end

-- Exact (case-insensitive) gem lookup. PoB's own FindSkillGem falls back to abbreviation patterns
-- ("Onslaught" -> "Frenzy of Onslaught", an active gem, when the support was renamed Momentum), which
-- silently puts a different gem in the link; a spec or variant must name the gem it means.
local function findGem(name)
	local want = lc(name)
	local alt = contains(want, "support") and (want:gsub("%s*support$", "")) or (want .. " support")
	local altHit
	for _, gemData in pairs(build.data.gems) do
		local n = lc(gemData.name)
		if n == want then return gemData end          -- the exact name wins ("Barrage Support" over "Barrage")
		if n == alt and (not altHit or (gemData.tags and gemData.tags.support)) then altHit = gemData end
	end
	if altHit then return altHit end
	return nil, "Unrecognised gem name '" .. name .. "'"
end

-- Fuzzy ranking: shared words (3+ letters) plus substring bonuses.
local function words(s)
	local w = {}
	for x in lc(s):gmatch("[%w']+") do
		x = x:gsub("'s$", ""):gsub("'", "")
		if #x >= 3 and x ~= "support" and x ~= "the" and x ~= "of" then w[x] = true end
	end
	return w
end
local function fuzzyTop(query, candidates, n)
	local qw, qn = words(query), norm(query)
	local scored, seen = {}, {}
	for _, name in ipairs(candidates) do
		if not seen[name] then
			seen[name] = true
			local score, cn = 0, norm(name)
			for x in pairs(words(name)) do
				if qw[x] then score = score + 2
				else
					for y in pairs(qw) do
						if #y >= 4 and #x >= 4 and (x:sub(1, 4) == y:sub(1, 4)) then score = score + 1 break end
					end
				end
			end
			if cn:find(qn, 1, true) or qn:find(cn, 1, true) then score = score + 3 end
			if score > 0 then t_insert(scored, { name = name, score = score }) end
		end
	end
	table.sort(scored, function(a, b)
		if a.score ~= b.score then return a.score > b.score end
		return #a.name < #b.name
	end)
	local out = {}
	for i = 1, math.min(n or 5, #scored) do out[i] = scored[i].name end
	return out
end

-- Stage support as PoB data describes it: a skill is multi-stage when its mods (base mods or
-- stat-mapped mods) define "Multiplier:<Name>MaxStages". The real limit can be raised by gear,
-- so this is evidence for the preflight; the calculated skill is the authority.
local function stageInfo(ge)
	if not ge then return false, nil end
	local evidence, baseMax = false, nil
	local function scan(list)
		for _, m in ipairs(list or {}) do
			if type(m) == "table" and type(m.name) == "string" and m.name:match("MaxStages$") then
				evidence = true
				if m.type == "BASE" and type(m.value) == "number" and not m[1] then
					baseMax = math.max(baseMax or 0, m.value)
				end
			end
		end
	end
	scan(ge.baseMods)
	for _, mods in pairs(ge.statMap or {}) do if type(mods) == "table" then scan(mods) end end
	return evidence, baseMax
end
local function partNames(ge)
	local names = {}
	for _, part in ipairs((ge and ge.parts) or {}) do t_insert(names, part.name or "?") end
	return names
end

local function gemSuggestions(name, n)
	local names = {}
	-- Awakened gems are drop-disabled since 3.29 (legacy, Standard only): never suggest them
	for _, g in pairs(build.data.gems) do if not (g.tags and g.tags.awakened) then t_insert(names, g.name) end end
	return fuzzyTop(name, names, n)
end

local function gemColorLetter(gemData)
	local c = gemData and gemData.grantedEffect and gemData.grantedEffect.color
	return c == 1 and "R" or c == 2 and "G" or c == 3 and "B" or "W"
end

-- title (lowercase) -> item; built from main.uniqueDB.list (works on PoB release and dev)
local uniqueIndex
local function uniquesByTitle()
	if uniqueIndex then return uniqueIndex end
	uniqueIndex = {}
	for _, it in pairs(main.uniqueDB.list) do
		if it.title then uniqueIndex[lc(it.title)] = it end
	end
	return uniqueIndex
end
local function uniqueByName(name)
	local idx = uniquesByTitle()
	local item = idx[lc(name)]
	if item then return item end
	for title, it in pairs(idx) do
		if norm(title) == norm(name) then return it end
	end
end
local function uniqueSuggestions(name, n)
	local names = {}
	for _, it in pairs(uniquesByTitle()) do t_insert(names, it.title) end
	return fuzzyTop(name, names, n)
end

---------------------------------------------------------------------------
-- Item mod legality
---------------------------------------------------------------------------
local patternCache = {}
local function templateToPattern(tpl)
	if patternCache[tpl] then return patternCache[tpl][1], patternCache[tpl][2] end
	local pat, ranges, i = {}, {}, 1
	while i <= #tpl do
		local s, e, a, b = tpl:find("^%((%-?[%d%.]+)%-(%-?[%d%.]+)%)", i)
		if s then
			t_insert(ranges, { tonumber(a), tonumber(b) })
			t_insert(pat, "(%-?[%d%.]+)")
			i = e + 1
		else
			local c = tpl:sub(i, i)
			t_insert(pat, (c:gsub("[%^%$%(%)%%%.%[%]%*%+%-%?]", "%%%0")))
			i = i + 1
		end
	end
	local p = "^" .. t_concat(pat) .. "$"
	patternCache[tpl] = { p, ranges }
	return p, ranges
end

-- Normalise an item mod line for matching: drop "(crafted)" style flags,
-- advanced-copy ranges like "+79(70-79)" and PoB "{range:..}" prefixes.
local function cleanModLine(line)
	line = line:gsub("^{[^}]*}", "")
	line = line:gsub("(%d)%((%-?[%d%.]+)%-(%-?[%d%.]+)%)", "%1")
	line = line:gsub(" %(%l+%)$", "")
	return trim(line)
end

-- The item's own mod line with its roll resolved. PoB's item crafter keeps a roll as range text plus a
-- {range:x} ("(31-36)% increased Damage while Leeching"); PoB resolves it with itemLib.applyRange
-- (Item.lua, getRangedModList), defaulting to main.defaultItemAffixQuality. Do the same, without
-- catalyst scaling, so the base roll is checked against the tier ranges like any other value.
local function rolledLine(ml)
	local line = ml.line or ""
	if line:find("%((%-?%d+%.?%d*)%-(%-?%d+%.?%d*)%)") then
		line = itemLib.applyRange(line:gsub("\n", " "), ml.range or (main and main.defaultItemAffixQuality) or 0.5)
	end
	return line
end

-- Does `line` match a mod template line? returns matched, inRange
local function matchModLine(line, tpl)
	local p, ranges = templateToPattern(tpl)
	local caps = { line:match(p) }
	if #caps == 0 then
		-- exact literal templates (no ranges)
		if #ranges == 0 and line == tpl then return true, true end
		return false, false
	end
	if #ranges == 0 then return true, true end
	for k, r in ipairs(ranges) do
		local v = tonumber(caps[k])
		if not v then return false, false end
		local lo, hi = math.min(r[1], r[2]), math.max(r[1], r[2])
		if v < lo - 1e-6 or v > hi + 1e-6 then return true, false end
	end
	return true, true
end

local function modLines(mod)
	local out = {}
	for _, l in ipairs(mod) do t_insert(out, l) end
	return out
end

-- PoB keeps every variant's lines on an item; only the selected variant's lines are real.
local function lineIsActive(item, ml)
	if ml.variantList and item.variant then
		if ml.variantList[item.variant] then return true end
		if item.hasAltVariant and item.variantAlt and ml.variantList[item.variantAlt] then return true end
		return false
	end
	return true
end

-- Lines that are themselves templates ("+(20-30) to maximum Life", as PoB stores
-- ranged implicits) are compared by shape, since any roll in their range is legal.
local function shapeOf(s)
	return (s:gsub("%(%-?[%d%.]+%-%-?[%d%.]+%)", "#"):gsub("%-?[%d%.]+", "#"))
end
local function implicitMatches(text, tpl)
	if text:find("%(%-?[%d%.]+%-%-?[%d%.]+%)") then
		if shapeOf(text) == shapeOf(tpl) then
			-- ranged line: every range must sit inside the template's range
			local _, tr = templateToPattern(tpl)
			local _, lr = templateToPattern(text)
			if #tr ~= #lr then return true, true end
			for k, r in ipairs(lr) do
				local lo, hi = math.min(tr[k][1], tr[k][2]), math.max(tr[k][1], tr[k][2])
				if math.min(r[1], r[2]) < lo - 1e-6 or math.max(r[1], r[2]) > hi + 1e-6 then return true, false end
			end
			return true, true
		end
		return false, false
	end
	return matchModLine(text, tpl)
end

local function splitLines(s)
	local out = {}
	for l in tostring(s or ""):gmatch("[^\n]+") do t_insert(out, l) end
	return out
end

-- Implicit templates the item's base type always has.
local function baseImplicitTemplates(item)
	return item.base and item.base.implicit and splitLines(item.base.implicit) or {}
end

-- Implicit templates of the unique's database entry (all variants).
local function uniqueImplicitTemplates(item)
	local db = item.title and uniqueByName(item.title)
	local out = {}
	if db then
		-- only the implicits of the variant this item is (legacy variants had other implicits)
		local variant = item.variant
		for _, ml in ipairs(db.implicitModLines or {}) do
			local inVariant = not (ml.variantList and variant) or ml.variantList[variant]
			if not ml.enchant and inVariant then t_insert(out, cleanModLine(ml.line)) end
		end
	end
	return out, db ~= nil
end

-- Corruption (Vaal) implicits for the item's class: the main pool plus the jewel pools
local function corruptedPools()
	local im = build.data.itemMods
	return { im.Corrupted or {}, im.Jewel or {}, im.JewelAbyss or {} }
end
local function corruptedImplicitMods(item)
	local out = {}
	for _, pool in ipairs(corruptedPools()) do
		for _, mod in pairs(pool) do
			if mod.type == "Corrupted" and item:GetModSpawnWeight(mod) > 0 then t_insert(out, mod) end
		end
	end
	return out
end

-- Validate the non-eldritch implicits (base implicits, unique implicits, corruption implicits).
--   Normal/Magic/Rare: base implicits; Exarch/Eater implicits replace them (helmet/body/gloves/boots);
--                      a Vaal orb can replace them with corruption implicits (then Corrupted).
--   Unique:           the unique's own implicits; if Corrupted, corruption implicits may replace them.
--                     Never Exarch/Eater implicits (checked in the eldritch block).
local function checkImplicits(item, report)
	local lines, hasEld = {}, false
	for _, ml in ipairs(item.implicitModLines or {}) do
		if not lineIsActive(item, ml) then
			-- inactive variant line
		elseif ml.exarch or ml.eater then
			hasEld = true
		elseif not (ml.enchant or ml.crucible or ml.scourge) then
			t_insert(lines, ml)
		end
	end
	local isUnique = item.rarity == "UNIQUE" or item.rarity == "RELIC"
	local allowed, src = {}, "base"
	local dbKnown = false
	if isUnique then
		allowed, dbKnown = uniqueImplicitTemplates(item)
		src = "unique"
		if not dbKnown then allowed = baseImplicitTemplates(item) src = "base" end
	else
		allowed = baseImplicitTemplates(item)
	end
	local corruptPool
	local counts = { base = 0, corrupted = 0 }
	for _, ml in ipairs(lines) do
		local text = cleanModLine(rolledLine(ml))
		local kind, hint
		local outOfRange
		for _, tpl in ipairs(allowed) do
			local m, inRange = implicitMatches(text, tpl)
			if m and inRange then kind = "base" break end
			if m then outOfRange = tpl end
		end
		if not kind then
			corruptPool = corruptPool or corruptedImplicitMods(item)
			for _, mod in ipairs(corruptPool) do
				for _, tpl in ipairs(modLines(mod)) do
					local m, inRange = implicitMatches(text, tpl)
					if m and inRange then kind = "corrupted" break end
				end
				if kind then break end
			end
		end
		if item.synthesised and not kind then
			t_insert(report.notes, "Synthesised implicit '" .. text .. "' not verified")
		elseif kind == "base" then
			counts.base = counts.base + 1
			if hasEld then
				t_insert(report.problems, "Base implicit '" .. text .. "' can't stay: Exarch/Eater implicits replace the item's implicits")
			end
		elseif kind == "corrupted" then
			counts.corrupted = counts.corrupted + 1
			if not item.corrupted then
				t_insert(report.problems, "'" .. text .. "' is a corruption implicit; the item must be Corrupted (add a 'Corrupted' line or \"corrupted\": true)")
			end
		elseif outOfRange then
			t_insert(report.problems, ("Implicit '%s': value out of range (%s implicit is '%s')"):format(text, src, outOfRange))
		else
			local expect = #allowed > 0 and t_concat(allowed, " / ") or "none"
			t_insert(report.problems, ("Implicit '%s' isn't possible here: %s implicits are [%s]%s"):format(
				text, isUnique and "this unique's" or (item.baseName .. "'s base"), expect,
				item.corrupted and "; it isn't a corruption implicit for this item class either" or ""))
		end
	end
	if counts.corrupted > 0 and counts.base > 0 then
		t_insert(report.problems, "A corruption implicit replaces the item's implicits; it can't sit alongside the " .. src .. " implicit")
	end
	if counts.corrupted > 2 then
		t_insert(report.problems, counts.corrupted .. " corruption implicits (max 2, and 2 only via double corruption)")
	elseif counts.corrupted == 2 then
		t_insert(report.notes, "2 corruption implicits: only possible through double corruption")
	end
	-- Missing implicits the item would have in game
	if not hasEld and #lines == 0 and #allowed > 0 and not item.corrupted then
		t_insert(report.notes, ("Missing %s implicit: '%s' (in game the item has it)"):format(src, t_concat(allowed, " / ")))
	end
	report.implicits = { base = counts.base, corrupted = counts.corrupted }
end

local function checkItemMods(item, slotName)
	local report = { slot = slotName, name = item.title or item.name, base = item.baseName, rarity = item.rarity, problems = {}, notes = {},
		base_type = item.base and item.base.type or nil }
	if not item.base then
		t_insert(report.problems, "Unknown base type '" .. tostring(item.baseName or "?") .. "'")
		return report
	end
	-- Searing Exarch / Eater of Worlds implicits (any rarity, uniques included)
	do
		local counts = { Exarch = 0, Eater = 0 }
		local pool = build.data.itemMods.Eldritch or {}
		-- A game import carries 'Searing Exarch Item' / 'Eater of Worlds Item' lines but no {eldritch} tag on the
		-- implicit lines themselves (PoB adds the tag only when the implicit is set in its UI). Classify untagged
		-- implicit lines on such an item against the eldritch pool first, so they are checked as what they are
		-- instead of being reported as impossible base implicits.
		if (item.cleansing or item.tangle) and item.rarity ~= "UNIQUE" and item.rarity ~= "RELIC" then
			local baseTpls = baseImplicitTemplates(item)
			for _, ml in ipairs(item.implicitModLines or {}) do
				if not (ml.exarch or ml.eater or ml.enchant or ml.crucible or ml.scourge) and lineIsActive(item, ml) then
					local text = cleanModLine(rolledLine(ml))
					local isBase = false
					for _, tpl in ipairs(baseTpls) do if implicitMatches(text, tpl) then isBase = true break end end
					if not isBase then
						for _, mod in pairs(pool) do
							local hit = false
							for _, tpl in ipairs(modLines(mod)) do if matchModLine(text, tpl) then hit = true break end end
							if hit and item:GetModSpawnWeight(mod) > 0 then
								if mod.type == "Exarch" and item.cleansing then ml.exarch = true break
								elseif mod.type == "Eater" and item.tangle then ml.eater = true break end
							end
						end
					end
				end
			end
		end
		for _, ml in ipairs(item.implicitModLines or {}) do
			if (ml.exarch or ml.eater) and lineIsActive(item, ml) then
				local want = ml.exarch and "Exarch" or "Eater"
				local text = cleanModLine(rolledLine(ml))
				counts[want] = counts[want] + 1
				if want == "Exarch" and not item.cleansing then
					t_insert(report.problems, "Exarch implicit '" .. text .. "' needs a 'Searing Exarch Item' line")
				elseif want == "Eater" and not item.tangle then
					t_insert(report.problems, "Eater implicit '" .. text .. "' needs an 'Eater of Worlds Item' line")
				end
				local ok, hint, best, bestTop, otherType = false, nil, nil, nil, nil
				for _, mod in pairs(pool) do
					for _, tpl in ipairs(modLines(mod)) do
						local m, inRange = matchModLine(text, tpl)
						if m then
							local w = item:GetModSpawnWeight(mod)
							if mod.type ~= want then
								otherType = otherType or mod.type
							elseif w > 0 and inRange then
								ok = true
							elseif w > 0 then
								local _, ranges = templateToPattern(tpl)
								local top = ranges[1] and math.max(math.abs(ranges[1][1]), math.abs(ranges[1][2])) or 0
								if not bestTop or top > bestTop then best, bestTop = tpl, top end
							end
						end
					end
					if ok then break end
				end
				if not ok then
					if best then
						hint = "value out of range; highest " .. want .. " tier on " .. item.base.type .. " is '" .. best .. "'"
					elseif otherType then
						hint = "that is a " .. otherType .. " implicit, not " .. want
					else
						hint = "not an " .. want .. " implicit that can appear on " .. item.base.type
					end
					t_insert(report.problems, want .. " implicit '" .. text .. "': " .. hint)
				end
			end
		end
		for kind, n in pairs(counts) do
			if n > 1 then t_insert(report.problems, ("%d %s implicits (max 1)"):format(n, kind)) end
		end
		-- Eldritch currency only works on Normal/Magic/Rare, non-corrupted, non-mirrored
		-- helmets, body armours, gloves and boots (mirrors PoB's own rule in ItemsTab).
		if counts.Exarch + counts.Eater > 0 then
			if item.rarity == "UNIQUE" or item.rarity == "RELIC" then
				t_insert(report.problems, "Searing Exarch / Eater of Worlds implicits can't be applied to unique items")
			elseif item.corrupted or item.mirrored then
				t_insert(report.problems, "Searing Exarch / Eater of Worlds implicits can't be applied to corrupted or mirrored items")
			end
		end
		report.eldritch = (counts.Exarch + counts.Eater > 0) and counts or nil
		-- the eldritch implicit texts, so a variant that replaces one can remove the old line from raw item text
		if counts.Exarch + counts.Eater > 0 then
			report.eldritch_lines = {}
			for _, ml in ipairs(item.implicitModLines or {}) do
				if (ml.exarch or ml.eater) and lineIsActive(item, ml) then report.eldritch_lines[ml.exarch and "Exarch" or "Eater"] = cleanModLine(rolledLine(ml)) end
			end
		end
	end
	checkImplicits(item, report)
	if item.rarity ~= "RARE" and item.rarity ~= "MAGIC" then return report end
	local ilvl = tonumber(item.itemLevel) or 86
	if not item.itemLevel then t_insert(report.notes, "No 'Item Level:' given; assumed 86 for affix checks") end
	local isJewel = item.base.type == "Jewel"
	local maxPrefix = item.rarity == "MAGIC" and 1 or (isJewel and 2 or 3)
	local maxSuffix = maxPrefix
	local prefixes, suffixes, crafted = 0, 0, 0
	local multimod = false

	-- candidate affix pools
	local explicitPool = item.affixes or build.data.itemMods.Item
	local craftPool = {}
	for _, mod in ipairs(build.data.masterMods) do
		if mod.types and (mod.types[item.base.type] or (item.base.subType and mod.types[item.base.type])) then
			t_insert(craftPool, mod)
		end
	end

	local lines = {}
	for _, ml in ipairs(item.explicitModLines) do
		t_insert(lines, { raw = ml.line, text = cleanModLine(rolledLine(ml)), crafted = ml.crafted, fractured = ml.fractured, used = false })
	end

	-- Tier signal (realism, not price). For each mod group that can roll on this base, the game's
	-- best tier is the one with the highest required item level (PoB's ModItem data), regardless of
	-- this item's level; a group with a single tier has no "top tier" to speak of. The old
	-- per-item-level measure is kept as top_tier_at_ilvl: it is relative to the item, so a low
	-- item level made ordinary rolls look top tier.
	local topLevel, topLevelAtIlvl, tierLevels = {}, {}, {}
	for _, mod in pairs(explicitPool) do
		if (mod.type == "Prefix" or mod.type == "Suffix") and mod.group and item:GetModSpawnWeight(mod) > 0 then
			local lvl = mod.level or 1
			topLevel[mod.group] = math.max(topLevel[mod.group] or 0, lvl)
			if lvl <= ilvl then topLevelAtIlvl[mod.group] = math.max(topLevelAtIlvl[mod.group] or 0, lvl) end
			tierLevels[mod.group] = tierLevels[mod.group] or {}
			tierLevels[mod.group][lvl] = true
		end
	end
	local function tierCount(group)
		local n = 0
		for _ in pairs(tierLevels[group] or {}) do n = n + 1 end
		return n
	end
	local topTiers, topTiersAtIlvl, explicitAffixes, maxAffixLevel = 0, 0, 0, 0
	local topTierMods = {}

	-- an item can carry at most one mod from each mod group (explicit or crafted)
	local usedGroups = {}
	local function tryPool(pool, isCraft)
		-- multi-line (hybrid) mods first so their lines are consumed together
		local entries = {}
		for id, mod in pairs(pool) do
			if (mod.type == "Prefix" or mod.type == "Suffix") then
				local w = isCraft and 1 or item:GetModSpawnWeight(mod)
				if w > 0 then t_insert(entries, mod) end
			end
		end
		table.sort(entries, function(a, b) return #a > #b end)
		for _, mod in ipairs(entries) do
			if mod.group and usedGroups[mod.group] then goto nextMod end
			local ml = modLines(mod)
			local hits, allOk = {}, true
			for _, tpl in ipairs(ml) do
				local found
				for li, l in ipairs(lines) do
					if not l.used and not hits[li] and (isCraft == (l.crafted and true or false)) then
						local m, inRange = matchModLine(l.text, tpl)
						if m and inRange then found = li break end
					end
				end
				if not found then allOk = false break end
				hits[found] = true
			end
			if allOk and next(hits) and (mod.level or 1) <= (isCraft and 100 or ilvl) then
				for li in pairs(hits) do
					lines[li].used = true
					lines[li].affix = mod.type
					lines[li].modName = mod.affix
					lines[li].tier = mod.level
					lines[li].hybrid = #ml > 1
					lines[li].group = mod.group
				end
				if mod.type == "Prefix" then prefixes = prefixes + 1 else suffixes = suffixes + 1 end
				if isCraft then
					crafted = crafted + 1
				else
					explicitAffixes = explicitAffixes + 1
					maxAffixLevel = math.max(maxAffixLevel, mod.level or 1)
					-- resistance, attribute and item-rarity tiers are the cheap top tiers (essences, harvest, bench
					-- crafts put T1 within any budget): they don't count toward "demanding rolls"
					local cheapGroup = mod.group and (mod.group:find("Resist") or mod.group:find("Strength") or mod.group:find("Dexterity")
						or mod.group:find("Intelligence") or mod.group:find("Attribute") or mod.group:find("Rarity")) and true or false
					if mod.group and tierCount(mod.group) > 1 then
						if (mod.level or 1) == topLevel[mod.group] and not cheapGroup then
							topTiers = topTiers + 1
							local txt = {}
							for li in pairs(hits) do t_insert(txt, lines[li].text) end
							table.sort(txt)
							t_insert(topTierMods, { text = table.concat(txt, " / "), tiers = tierCount(mod.group), item_level = mod.level or 1 })
						end
						if (mod.level or 1) == topLevelAtIlvl[mod.group] then topTiersAtIlvl = topTiersAtIlvl + 1 end
					end
				end
				if mod.group then usedGroups[mod.group] = mod.type end
				for _, tpl in ipairs(ml) do
					if contains(tpl, "Can have up to 3 Crafted Modifiers") then multimod = true end
				end
			end
			::nextMod::
		end
	end
	tryPool(explicitPool, false)
	tryPool(craftPool, true)
	-- Legality-aware re-assignment: a line that matched a prefix may also exist as a suffix (item rarity, some
	-- resistance/attribute hybrids). The greedy pass takes the first match; if that overfills one side while the
	-- other has room, move single-line mods across where an in-range mod of the other type exists.
	local function reassign(fromType, toType, over)
		for _, l in ipairs(lines) do
			if over <= 0 then break end
			if l.used and l.affix == fromType and not l.crafted and not l.hybrid then
				for _, mod in pairs(explicitPool) do
					if mod.type == toType and #mod == 1 and item:GetModSpawnWeight(mod) > 0 and (mod.level or 1) <= ilvl
						and not (mod.group and usedGroups[mod.group]) then
						local m, inRange = matchModLine(l.text, modLines(mod)[1])
						if m and inRange then
							if l.group and usedGroups[l.group] then usedGroups[l.group] = nil end
							l.affix, l.modName, l.tier = toType, mod.affix, mod.level
							if mod.group then usedGroups[mod.group] = toType end
							if fromType == "Prefix" then prefixes = prefixes - 1 suffixes = suffixes + 1 else suffixes = suffixes - 1 prefixes = prefixes + 1 end
							over = over - 1
							break
						end
					end
				end
			end
		end
	end
	if prefixes > maxPrefix and suffixes < maxSuffix then reassign("Prefix", "Suffix", prefixes - maxPrefix) end
	if suffixes > maxSuffix and prefixes < maxPrefix then reassign("Suffix", "Prefix", suffixes - maxSuffix) end
	if os.getenv("POE1_BUILDS_ITEM_TRACE") then
		report.line_trace = {}
		for _, l in ipairs(lines) do t_insert(report.line_trace, { text = l.text, affix = l.affix, mod = l.modName, tier = l.tier, crafted = l.crafted and true or false, used = l.used }) end
	end

	-- explain anything left unmatched. Priority of explanations:
	--   3: rollable here but value too high/low (report best tier at this ilvl)
	--   2: only a higher-ilvl tier has this value
	--   1: this kind of mod can't roll on this base at all
	for _, l in ipairs(lines) do
		if not l.used then
			local bestHint, bestPrio = nil, 0
			local bestTier, bestTierScore
			local pool = l.crafted and craftPool or explicitPool
			for _, mod in pairs(pool) do
				if (mod.type == "Prefix" or mod.type == "Suffix") then
					for _, tpl in ipairs(modLines(mod)) do
						local m, inRange = matchModLine(l.text, tpl)
						if m then
							local w = l.crafted and 1 or item:GetModSpawnWeight(mod)
							local lvl = mod.level or 1
							if w > 0 and lvl <= ilvl and inRange and mod.group and usedGroups[mod.group] and bestPrio < 4 then
								bestPrio, bestHint = 4, "duplicate of another mod in the same group ('" .. mod.group .. "'); an item can only have one"
							elseif w > 0 and lvl <= ilvl then
								-- best = highest upper value, preferring single-line (non-hybrid) mods
								local _, ranges = templateToPattern(tpl)
								local top = ranges[1] and math.max(math.abs(ranges[1][1]), math.abs(ranges[1][2])) or 0
								local score = top + (#mod == 1 and 1e6 or 0)
								if not bestTierScore or score > bestTierScore then bestTier, bestTierScore = tpl, score end
								if bestPrio < 3 and not inRange then bestPrio = 3 end
							elseif w > 0 and inRange and bestPrio < 2 then
								bestPrio, bestHint = 2, "this value needs item level " .. lvl .. " (item is " .. ilvl .. ")"
							elseif w == 0 and bestPrio < 1 then
								bestPrio, bestHint = 1, "this mod can't roll on " .. item.base.type .. " (" .. item.baseName .. ")"
							end
						end
					end
				end
			end
			if bestPrio == 3 then
				bestHint = "value is outside the rollable range; best tier at item level " .. ilvl .. " is '" .. bestTier .. "'"
			end
			if l.crafted then
				t_insert(report.problems, "Crafted mod '" .. l.text .. "': " .. (bestHint or "not a bench craft for this item type"))
			elseif bestHint then
				t_insert(report.problems, "'" .. l.text .. "': " .. bestHint)
			else
				t_insert(report.notes, "'" .. l.text .. "' is not a regular affix for this base (essence/veiled/eldritch/special mods are possible but not verified)")
			end
		end
	end
	if prefixes > maxPrefix then t_insert(report.problems, ("%d prefixes (max %d)"):format(prefixes, maxPrefix)) end
	if suffixes > maxSuffix then t_insert(report.problems, ("%d suffixes (max %d)"):format(suffixes, maxSuffix)) end
	if crafted > 1 and not multimod then t_insert(report.problems, crafted .. " crafted mods without 'Can have up to 3 Crafted Modifiers'") end
	report.prefixes, report.suffixes = prefixes, suffixes
	report.top_tier_affixes, report.explicit_affixes = topTiers, explicitAffixes
	report.top_tier_at_ilvl, report.max_affix_item_level = topTiersAtIlvl, maxAffixLevel
	table.sort(topTierMods, function(a, b) return a.text < b.text end)   -- pool iteration order varies
	report.top_tier_mods = topTierMods
	report.item_level, report.item_level_assumed = ilvl, (not item.itemLevel) or nil
	report.base_type = item.base.type
	local signals = {}
	if item.cleansing or item.tangle then t_insert(signals, "eldritch implicits") end
	for _, inf in ipairs((itemLib.influenceInfo or {}).default or {}) do
		if item[inf.key] then t_insert(signals, inf.display .. " influence") end
	end
	if crafted > 0 then t_insert(signals, crafted .. " bench craft" .. (crafted > 1 and "s" or "")) end
	if multimod then t_insert(signals, "multimod craft") end
	for _, l in ipairs(lines) do if l.fractured then t_insert(signals, "fractured mod") break end end
	if item.corrupted then t_insert(signals, "corrupted") end
	report.crafting_signals = signals
	return report
end

---------------------------------------------------------------------------
-- Tree construction
---------------------------------------------------------------------------
local function findTreeNodes(target, wantAscendancy)
	local spec = build.spec
	local matches = {}
	if type(target) == "number" or (type(target) == "string" and target:match("^%d+$")) then
		local node = spec.nodes[tonumber(target)]
		if node then t_insert(matches, node) end
		return matches
	end
	local q = norm(target)
	for _, node in pairs(spec.nodes) do
		if isRealNode(node) and node.dn and norm(node.dn) == q then
			local isAsc = node.ascendancyName ~= nil
			if isAsc == (wantAscendancy and true or false) then
				if not isAsc or node.ascendancyName == spec.curAscendClassName
					or (spec.curSecondaryAscendClassName and node.ascendancyName == spec.curSecondaryAscendClassName) then
					t_insert(matches, node)
				end
			end
		end
	end
	return matches
end

local function nodeSuggestions(target, n, isAsc)
	local names = {}
	for _, node in pairs(build.spec.nodes) do
		if isRealNode(node) and (node.type == "Notable" or node.type == "Keystone") and node.dn
			and ((node.ascendancyName ~= nil) == (isAsc and true or false)) then
			t_insert(names, node.dn)
		end
	end
	return fuzzyTop(target, names, n)
end

---------------------------------------------------------------------------
-- Main-tree pathing: Steiner-style target connection with lookahead, path choice, and pruning
---------------------------------------------------------------------------
-- The passive tree problem is a Steiner tree problem: connect the named targets to the class start with
-- the fewest nodes. PoB's node.path is the shortest route from the NEAREST allocated node, so taking the
-- nearest target first (plain greedy) ignores that two targets may share a corridor and that an equal-
-- length route through useful nodes beats one through pure attributes. The code below keeps PoB as the
-- source of truth for allocation (spec:AllocNode with an explicit path) and does its own BFS for the
-- decisions. Rules mirror BuildPathFromNode: no passing through class/ascendancy start nodes, no passing
-- through masteries (endpoints only), main tree only (ascendancy targets use the simple greedy below).

-- A node the main-tree path may pass through.
local function pathable(node)
	if node.ascendancyName or node.isProxy or node.isBlighted or node.isMultipleChoiceOption then return false end
	if node.type == "ClassStart" or node.type == "AscendClassStart" or node.type == "Mastery" then return false end
	return true
end

-- Pure attribute travel nodes are the least valuable way to spend a point; small static penalty used as a
-- tie-break between equal-length routes (the fill later measures real value with PoB).
local function travelPenalty(node)
	if node.type ~= "Normal" then return 0 end
	local attrOnly = true
	for _, l in ipairs(node.sd or {}) do
		local t = l:lower()
		if not (t:match("^%+%d+ to strength$") or t:match("^%+%d+ to dexterity$") or t:match("^%+%d+ to intelligence$")
			or t:match("^%+%d+ to strength and ") or t:match("^%+%d+ to dexterity and ") or t:match("^%+%d+ to all attributes$")) then
			attrOnly = false
		end
	end
	return attrOnly and 0.5 or 0
end

-- Everything below works on a hypothetical allocation `free` (set of nodes counted as allocated: the real
-- main-tree allocation plus whatever a strategy has added), so whole strategies run without touching PoB.

local function canonical(spec, other) return other and other.id and spec.nodes[other.id] or other end
local connectedSet   -- forward declaration (defined below, used by treeBFS)

-- 0-1 BFS from every node in `free`: dist[n] = points needed to reach n (a node in `free` costs 0).
local function treeBFS(spec, free, sources)
	local dist = {}
	local dq, head, tail = {}, 1, 0
	local function push_front(n) head = head - 1 dq[head] = n end
	local function push_back(n) tail = tail + 1 dq[tail] = n end
	if sources then for _, n in ipairs(sources) do dist[n] = 0 push_back(n) end
	else
		-- only the part of `free` that reaches the class start: a detached island is not a place to path from
		local comp = connectedSet(spec, free)
		local list = {}
		for n in pairs(free) do if comp[n] then list[#list + 1] = n end end
		table.sort(list, function(a, b) return a.id < b.id end)
		for _, n in ipairs(list) do dist[n] = 0 push_back(n) end
	end
	while head <= tail do
		local node = dq[head] dq[head] = nil head = head + 1
		local d = dist[node]
		-- never expand THROUGH a start node (a source start node has d == 0 and may be expanded from) or a mastery
		if node.type ~= "Mastery" and (node.type ~= "ClassStart" or d == 0) then
			for _, other in ipairs(node.linked or {}) do
				other = canonical(spec, other)
				-- an allocated class start is a legal endpoint (the route ends there), never a waypoint
				if other and (pathable(other) or (other.type == "Mastery" and not other.ascendancyName)
					or (other.type == "ClassStart" and free[other])) then
					local nd = d + (free[other] and 0 or 1)
					if dist[other] == nil or nd < dist[other] then
						dist[other] = nd
						if free[other] then push_front(other) else push_back(other) end
					end
				end
			end
		end
	end
	return dist
end

-- The real main-tree allocation as a `free` set (class start included).
local function allocatedSet(spec)
	local free = {}
	for _, n in pairs(spec.allocNodes) do
		if not n.ascendancyName and (n.type == "ClassStart" or pathable(n) or n.type == "Mastery") then free[n] = true end
	end
	return free
end

local function copySet(t) local c = {} for k, v in pairs(t) do c[k] = v end return c end

-- The best shortest path (list of nodes not in `free`, target first) from `free` to `target`, given dS =
-- treeBFS(free). Among equal-length routes: nodes close to the other pending targets (`pull`: node ->
-- distance to the nearest other target) and the least pure-attribute travel. DP over the layers of dS.
local function bestPath(spec, target, dS, pull, free)
	local total = dS[target]
	if total == nil then return nil end
	local function cost(n) return free[n] and 0 or 1 end
	local dT = treeBFS(spec, free, { target })
	-- n lies on some shortest route iff reaching n, then n -> target without n, costs `total`
	local layer = {}
	for n, a in pairs(dS) do
		local b = dT[n]
		if b and a + b - cost(n) + cost(target) == total then layer[#layer + 1] = n end
	end
	table.sort(layer, function(x, y) if dS[x] ~= dS[y] then return dS[x] < dS[y] end return x.id < y.id end)
	local function weight(n)
		if free[n] then return 0 end
		local w = 1 + travelPenalty(n)
		if pull then local pd = pull[n] if pd then w = w + math.min(pd, 8) / 16 end end
		return w
	end
	local best, prev = {}, {}
	for _, n in ipairs(layer) do
		if dS[n] == 0 then best[n] = 0
		else
			local bw, bp
			for _, other in ipairs(n.linked or {}) do
				other = canonical(spec, other)
				if other and best[other] and other.type ~= "Mastery" and dS[other] == dS[n] - cost(n) then
					if not bw or best[other] < bw or (best[other] == bw and other.id < bp.id) then bw, bp = best[other], other end
				end
			end
			if bw then best[n] = bw + weight(n) prev[n] = bp end
		end
	end
	if not best[target] then return nil end
	local path = {}
	local n = target
	while n and dS[n] > 0 do
		if not free[n] then t_insert(path, n) end
		n = prev[n]
	end
	return path
end

-- The nodes of `free` reachable from the class start (skipping `without`): an island left behind by a
-- re-route is NOT part of the tree, whatever `free` says.
function connectedSet(spec, free, without)
	local start
	for n in pairs(free) do if n.type == "ClassStart" then start = n break end end
	if not start then return {} end
	local seen, stack = { [start] = true }, { start }
	while #stack > 0 do
		local node = table.remove(stack)
		if node.type ~= "Mastery" then
			for _, other in ipairs(node.linked or {}) do
				other = canonical(spec, other)
				if other and free[other] and other ~= without and not seen[other] and not other.ascendancyName
					and other.type ~= "ClassStart" and other.type ~= "AscendClassStart" then
					seen[other] = true t_insert(stack, other)
				end
			end
		end
	end
	return seen
end

-- Does every node in `protected` reach the class start through `free` (skipping `without`)?
local function allConnected(spec, free, protected, without)
	local start
	for n in pairs(free) do if n.type == "ClassStart" then start = n break end end
	if not start then return false end
	local seen, stack = { [start] = true }, { start }
	while #stack > 0 do
		local node = table.remove(stack)
		if node.type ~= "Mastery" then
			for _, other in ipairs(node.linked or {}) do
				other = canonical(spec, other)
				if other and free[other] and other ~= without and not seen[other] and not other.ascendancyName
					and other.type ~= "ClassStart" and other.type ~= "AscendClassStart" then
					seen[other] = true t_insert(stack, other)
				end
			end
		end
	end
	for n in pairs(protected) do if n ~= without and not seen[n] then return false end end
	return true
end

-- Remove from `free` every plain travel node (type Normal, not protected, not in `base`) whose removal keeps
-- all protected nodes connected. Worst travel (attribute-only) goes first. Returns the removed nodes.
local function simPrune(spec, free, protected, base)
	local removed = {}
	local changed = true
	while changed do
		changed = false
		local cands = {}
		for n in pairs(free) do
			if not protected[n] and not (base and base[n]) and pathable(n) and (n.type == "Normal" or n.type == "Notable") then t_insert(cands, n) end
		end
		table.sort(cands, function(a, b) local pa, pb = travelPenalty(a), travelPenalty(b) if pa ~= pb then return pa > pb end return a.id < b.id end)
		for _, n in ipairs(cands) do
			if free[n] and allConnected(spec, free, protected, n) then
				free[n] = nil
				t_insert(removed, n)
				changed = true
			end
		end
	end
	-- whatever is left detached from the start (a travel chain cut off by a re-route) goes too
	local comp = connectedSet(spec, free)
	local islands = {}
	for n in pairs(free) do if not comp[n] and not protected[n] and not (base and base[n]) and n.type ~= "ClassStart" then islands[#islands + 1] = n end end
	table.sort(islands, function(a, b) return a.id < b.id end)
	for _, n in ipairs(islands) do free[n] = nil t_insert(removed, n) end
	return removed
end

local function setCost(free, base)
	local c, pen = 0, 0
	for n in pairs(free) do if not base[n] then c = c + 1 pen = pen + travelPenalty(n) end end
	return c, pen
end

-- Connect the pending targets to `free` in the order a strategy dictates. `order` = "nearest" (plain greedy,
-- PoB's behaviour), "farthest" (the costliest target first: the trunk goes out once, the rest hang off it),
-- "lookahead" (own cost plus everyone else's shortest paths afterwards). Returns the plan (target, node, cost)
-- and the protected set; mutates `free`.
local function simConnect(spec, free, pending, order)
	local plan, protected = {}, {}
	local todo = {}
	for i, p in ipairs(pending) do todo[i] = p end
	while #todo > 0 do
		local dS = treeBFS(spec, free)
		local info = {}
		for i, p in ipairs(todo) do
			local bestN, bestD
			for _, node in ipairs(p.candidates) do
				if free[node] then bestN, bestD = node, 0 break end
				local d = dS[node]
				if d and (not bestD or d < bestD or (d == bestD and node.id < bestN.id)) then bestN, bestD = node, d end
			end
			info[i] = { node = bestN, d = bestD }
		end
		local pick
		for i, inf in ipairs(info) do if inf.d == 0 then pick = i break end end
		if not pick then
			local bestKey
			for i, inf in ipairs(info) do
				if inf.node then
					local key
					if order == "nearest" then key = inf.d
					elseif order == "farthest" then key = -inf.d
					else
						local path = bestPath(spec, inf.node, dS, nil, free) or {}
						local f2 = copySet(free)
						for _, n in ipairs(path) do f2[n] = true end
						local d2 = treeBFS(spec, f2)
						key = inf.d
						for j, other in ipairs(info) do
							if j ~= i and other.node and other.d and other.d > 0 then
								local md
								for _, c in ipairs(todo[j].candidates) do local dd = d2[c] if dd and (not md or dd < md) then md = dd end end
								key = key + (md or other.d)
							end
						end
						key = key + inf.d / 1000   -- tie: cheaper own step first
					end
					if not bestKey or key < bestKey then bestKey, pick = key, i end
				end
			end
		end
		if not pick then
			if os.getenv("POE1_BUILDS_PATH_TRACE") then
				local tf = io.open(os.getenv("POE1_BUILDS_PATH_TRACE"), "a")
				for i, p in ipairs(todo) do
					local c = p.candidates[1]
					tf:write(("[path] unreachable target %s (first candidate id=%s type=%s asc=%s linked=%d dS=%s)\n"):format(p.label, tostring(c and c.id), tostring(c and c.type), tostring(c and c.ascendancyName), c and #(c.linked or {}) or -1, tostring(c and dS[c])))
				end
				tf:close()
			end
			return nil   -- something can't be connected: the caller falls back to PoB
		end
		local p = table.remove(todo, pick)
		local inf = info[pick]
		if inf.d > 0 then
			local pull
			if #todo > 0 then
				local others = {}
				for _, q in ipairs(todo) do for _, c in ipairs(q.candidates) do others[#others + 1] = c end end
				pull = treeBFS(spec, free, others)
			end
			local path = bestPath(spec, inf.node, dS, pull, free)
			if not path then
				if os.getenv("POE1_BUILDS_PATH_TRACE") then local tf = io.open(os.getenv("POE1_BUILDS_PATH_TRACE"), "a") tf:write(("[path] bestPath failed for %s (dS=%s)\n"):format(p.label, tostring(inf.d))) tf:close() end
				return nil
			end
			for _, n in ipairs(path) do free[n] = true end
		end
		protected[inf.node] = true
		t_insert(plan, { target = p.label, id = inf.node.id, name = inf.node.dn, type = inf.node.type, cost = inf.d })
	end
	return plan, protected
end

-- Local search: for each target in turn, drop the travel only it uses (prune with it unprotected), then
-- reconnect it from what is left. Keep the result when it is cheaper. Repeats until a full pass finds nothing.
local function simImprove(spec, free, protected, base, plan)
	local targets = {}
	for n in pairs(protected) do targets[#targets + 1] = n end
	table.sort(targets, function(a, b) return a.id < b.id end)
	local improved = true
	local rounds = 0
	while improved and rounds < 6 do
		improved = false
		rounds = rounds + 1
		for _, t in ipairs(targets) do
			-- only a target whose removal leaves every other target connected can be re-routed on its own
			-- (a target that is a corridor to others would take them with it)
			if not base[t] and allConnected(spec, free, protected, t) then
				local trial = copySet(free)
				local prot = copySet(protected)
				prot[t] = nil
				trial[t] = nil
				simPrune(spec, trial, prot, base)
				local dS = treeBFS(spec, trial)
				local path = bestPath(spec, t, dS, nil, trial)
				if path then
					for _, n in ipairs(path) do trial[n] = true end
					local c0, p0 = setCost(free, base)
					local c1, p1 = setCost(trial, base)
					if (c1 < c0 or (c1 == c0 and p1 < p0)) and allConnected(spec, trial, protected, nil) then
						for k in pairs(free) do free[k] = nil end
						for k, v in pairs(trial) do free[k] = v end
						improved = true
					end
				end
			end
		end
	end
	return rounds
end

local ATTR_WORDS = { Str = "strength", Dex = "dexterity", Int = "intelligence" }
local function attrFromNode(node, attr)
	local total = 0
	for _, l in ipairs(node.sd or {}) do
		local t = l:lower()
		local n = t:match("^%+(%d+) to " .. ATTR_WORDS[attr]) or t:match("^%+(%d+) to all attributes")
			or t:match("^%+(%d+) to %a+ and " .. ATTR_WORDS[attr]) or t:match("^%+(%d+) to " .. ATTR_WORDS[attr] .. " and ")
		if n then total = total + tonumber(n) end
	end
	return total
end
TARGET_IDS = {}   -- node ids the spec named (targets): never pruned
PATHING = nil     -- summary of the pathing search for the report
PINNED = false    -- tree.allocated given: PoB's own allocation, untouched

-- Main-tree targets: simulate the strategies, prune and locally improve each, apply the cheapest to PoB.
local function allocateMainTargets(pending, plan, ref, base)
	local spec = build.spec
	base = base or allocatedSet(spec)
	local results = {}
	-- reference: what PoB's own nearest-first allocation produced (run for real by the caller and still live;
	-- `base` is the allocation before it)
	local rawFree, rawCost = ref and ref.free or nil, ref and ref.cost or nil
	for _, order in ipairs({ "nearest", "lookahead", "farthest" }) do
		local free = copySet(base)
		local p, protected = simConnect(spec, free, pending, order)
		if p then
			local function chk(stage)
				if os.getenv("POE1_BUILDS_PATH_TRACE") and not allConnected(spec, free, protected, nil) then
					local tf = io.open(os.getenv("POE1_BUILDS_PATH_TRACE"), "a") tf:write(("[path] %s: disconnected after %s\n"):format(order, stage)) tf:close()
				end
			end
			chk("connect")
			local pruned = simPrune(spec, free, protected, base)
			chk("prune")
			local rounds = simImprove(spec, free, protected, base, p)
			chk("improve")
			simPrune(spec, free, protected, base)
			chk("prune2")
			-- sanity: every target connected to the start in the final set, or the strategy is discarded
			if allConnected(spec, free, protected, nil) then
				local c, pen = setCost(free, base)
				t_insert(results, { order = order, free = free, plan = p, protected = protected, cost = c, penalty = pen, pruned = #pruned, rounds = rounds })
			elseif os.getenv("POE1_BUILDS_PATH_TRACE") then
				local tf = io.open(os.getenv("POE1_BUILDS_PATH_TRACE"), "a")
				local comp = connectedSet(spec, free)
				for n in pairs(protected) do if not comp[n] then tf:write(("[path] strategy %s: target %s (%d) not connected at the end\n"):format(order, tostring(n.dn), n.id)) end end
				tf:close()
			end
		end
	end
	if #results == 0 then return false end
	table.sort(results, function(a, b) if a.cost ~= b.cost then return a.cost < b.cost end if a.penalty ~= b.penalty then return a.penalty < b.penalty end return a.order < b.order end)
	local win = results[1]
	-- No saving: keep PoB's own route. Equal-cost routes differ in what they bring within the fill's reach (a
	-- charge notable, a life cluster), which no static tie-break can see; the known route is the safer default.
	if rawCost and win.cost >= rawCost then
		local alt = {}
		for _, r in ipairs(results) do t_insert(alt, ("%s %d"):format(r.order, r.cost)) end
		PATHING = { strategy = "nearest (PoB)", total_points = rawCost, strategies = alt, nearest_points = rawCost, kept_pob_route = true,
			attribute_travel_dropped = 0, attribute_delta = { Str = 0, Dex = 0, Int = 0 } }
		return false
	end
	-- apply: first undo the reference allocation (still live), then allocate exactly the winning set, in id order
	-- (PoB iterates allocNodes in insertion order when it builds paths, so address-ordered insertion would make
	-- equal-length path choices differ between runs)
	if rawFree then
		for n in pairs(rawFree) do if not base[n] then n.alloc = false spec.allocNodes[n.id] = nil end end
		spec:BuildAllDependsAndPaths()
	end
	local toAlloc = {}
	for n in pairs(win.free) do if not n.alloc then toAlloc[#toAlloc + 1] = n end end
	table.sort(toAlloc, function(a, b) return a.id < b.id end)
	for _, n in ipairs(toAlloc) do n.alloc = true spec.allocNodes[n.id] = n end
	spec:BuildAllDependsAndPaths()
	-- PoB deallocates anything it finds disconnected: if it disagrees with the simulation, undo and let the
	-- caller fall back to PoB's own allocation rather than deliver a tree missing a target
	local dropped = 0
	for _, n in ipairs(toAlloc) do if not n.alloc then dropped = dropped + 1 end end
	if dropped > 0 then
		for _, n in ipairs(toAlloc) do if n.alloc then n.alloc = false spec.allocNodes[n.id] = nil end end
		if rawFree then
			local back = {}
			for n in pairs(rawFree) do if not base[n] then back[#back + 1] = n end end
			table.sort(back, function(a, b) return a.id < b.id end)
			for _, n in ipairs(back) do n.alloc = true spec.allocNodes[n.id] = n end
		end
		spec:BuildAllDependsAndPaths()
		note(("Pathing search produced a tree PoB rejects (%d node%s disconnected); PoB's nearest-first pathing used instead"):format(dropped, dropped == 1 and "" or "s"))
		return false
	end
	if os.getenv("POE1_BUILDS_PATH_TRACE") then
		local tf = io.open(os.getenv("POE1_BUILDS_PATH_TRACE"), "a")
		for _, n in ipairs(toAlloc) do
			if not n.alloc then
				local nb = {}
				for _, o in ipairs(n.linked or {}) do o = canonical(spec, o) nb[#nb + 1] = ("%d:%s%s%s"):format(o.id, o.type, o.alloc and "+" or "-", win.free[o] and "F" or "") end
				tf:write(("[path] PoB DROPPED %d %s type=%s linked={%s}\n"):format(n.id, tostring(n.dn), n.type, t_concat(nb, " ")))
			end
		end
		tf:close()
	end
	-- per-target cost in the report: the strategy's own step costs sum to more than the final tree when the
	-- improvement pass re-routed; scale is kept honest by also reporting the total
	for _, e in ipairs(win.plan) do TARGET_IDS[e.id] = true t_insert(plan, e) end
	local alt = {}
	for _, r in ipairs(results) do t_insert(alt, ("%s %d"):format(r.order, r.cost)) end
	-- attribute travel the winning route no longer takes: a build that leaned on it for a gem requirement
	-- now needs a deliberate attribute node or suffix (step 5 of the skill)
	local lostAttr = 0
	-- attributes the nearest-first route would have supplied and this one doesn't (per attribute, from node text):
	-- the attribute repair covers at most this much, so a shortfall the spec had anyway stays an error
	local attrDelta = { Str = 0, Dex = 0, Int = 0 }
	if rawFree then
		for n in pairs(rawFree) do
			if not win.free[n] then
				if travelPenalty(n) > 0 then lostAttr = lostAttr + 1 end
				for a in pairs(attrDelta) do attrDelta[a] = attrDelta[a] + attrFromNode(n, a) end
			end
		end
		for n in pairs(win.free) do
			if not rawFree[n] then for a in pairs(attrDelta) do attrDelta[a] = attrDelta[a] - attrFromNode(n, a) end end
		end
	end
	if os.getenv("POE1_BUILDS_PATH_TRACE") and rawFree then
		local tf = io.open(os.getenv("POE1_BUILDS_PATH_TRACE"), "a")
		for n in pairs(rawFree) do if not win.free[n] then tf:write(("[path] greedy-only %d %s | %s\n"):format(n.id, tostring(n.dn), t_concat(n.sd or {}, " / "))) end end
		for n in pairs(win.free) do if not rawFree[n] then tf:write(("[path] search-only %d %s | %s\n"):format(n.id, tostring(n.dn), t_concat(n.sd or {}, " / "))) end end
		tf:close()
	end
	PATHING = { strategy = win.order, total_points = win.cost, strategies = alt, pruned_in_sim = win.pruned, improve_rounds = win.rounds,
		nearest_points = rawCost or win.cost, attribute_travel_dropped = lostAttr, attribute_delta = attrDelta }
	return true
end

-- Real-tree prune (after the fill): the same pass on the live allocation, applied to PoB.
local function pruneTravel(spec, protected)
	local free = allocatedSet(spec)
	local removed = simPrune(spec, free, protected, nil)
	for _, n in ipairs(removed) do n.alloc = false spec.allocNodes[n.id] = nil end
	if #removed > 0 then spec:BuildAllDependsAndPaths() end
	return removed
end

-- Protected set for pruning: everything allocated that is not a plain travel small (keystones, notables,
-- sockets, masteries), plus any ids given.
local function protectedSet(spec, extraIds)
	local prot = {}
	for _, n in pairs(spec.allocNodes) do
		-- keystones, sockets (a jewel may sit there), masteries, ascendancy and cluster nodes; a plain notable is
		-- travel unless it is named or fill-chosen (extraIds)
		if n.type ~= "Normal" and n.type ~= "Notable" then prot[n] = true end
		if n.ascendancyName or n.isProxy or n.id >= 65536 then prot[n] = true end
	end
	for id in pairs(extraIds or {}) do local n = spec.nodes[id] if n then prot[n] = true end end
	return prot
end

-- Greedy: repeatedly allocate the cheapest-to-reach remaining target (ascendancy trees; also the fallback).
local function allocateTargets(targets, isAsc, plan)
	local spec = build.spec
	local pending = {}
	for _, t in ipairs(targets) do
		local label = type(t) == "table" and (t.name or t.id) or t
		local key = type(t) == "table" and (t.id or t.name) or t
		local m = findTreeNodes(key, isAsc)
		if #m == 0 then
			local sug = nodeSuggestions(tostring(key), 5, isAsc)
			err(("Passive '%s' not found%s%s"):format(tostring(label), isAsc and (" in " .. tostring(spec.curAscendClassName)) or "",
				#sug > 0 and (" (did you mean: " .. t_concat(sug, ", ") .. "?)") or ""))
		else
			t_insert(pending, { label = tostring(label), candidates = m })
		end
	end
	-- PoB's nearest-first allocation (the original behaviour; still used for ascendancy trees and as the fallback)
	local function greedy(list, out)
		while #list > 0 do
			local bestIdx, bestNode, bestDist
			for i, p in ipairs(list) do
				for _, node in ipairs(p.candidates) do
					if node.alloc then
						bestIdx, bestNode, bestDist = i, node, 0
						break
					end
					if node.path and (not bestDist or #node.path < bestDist) then
						bestIdx, bestNode, bestDist = i, node, #node.path
					end
				end
				if bestDist == 0 then break end
			end
			if not bestIdx then
				for _, p in ipairs(list) do
					err(("Passive '%s' can't be connected to the tree"):format(p.label))
				end
				break
			end
			local p = table.remove(list, bestIdx)
			if not bestNode.alloc then
				spec:AllocNode(bestNode)
			end
			t_insert(out, { target = p.label, id = bestNode.id, name = bestNode.dn, type = bestNode.type, cost = bestDist })
		end
	end
	if not isAsc and #pending > 0 and not PINNED and not os.getenv("POE1_BUILDS_GREEDY_PATHING") then
		-- 1. the nearest-first allocation for real: the reference for the point count and for the attributes its
		--    travel happened to supply (the simulation can't reproduce PoB's tie-breaks exactly)
		local base = allocatedSet(spec)
		local copy = {}
		for i, p in ipairs(pending) do copy[i] = p end
		local greedyPlan = {}
		greedy(copy, greedyPlan)
		local ref = { free = allocatedSet(spec), cost = 0 }
		for n in pairs(ref.free) do if not base[n] then ref.cost = ref.cost + 1 end end
		-- 2. the search runs on the graph only; it undoes the greedy allocation itself if it wins
		local searchPlan = {}
		if allocateMainTargets(pending, searchPlan, ref, base) then
			for _, e in ipairs(searchPlan) do t_insert(plan, e) end
			return
		end
		-- 3. no saving (or no connected result): the greedy allocation is still live and stands as the tree
		if not (PATHING and PATHING.kept_pob_route) then
			note("Steiner pathing could not connect every target; using PoB's nearest-first pathing")
		end
		for _, e in ipairs(greedyPlan) do TARGET_IDS[e.id] = true t_insert(plan, e) end
		return
	end
	greedy(pending, plan)
end

local function allocateMasteries(list, plan)
	local spec = build.spec
	local used = {}
	for _, want in ipairs(list or {}) do
		local effectText = type(want) == "table" and (want.effect or want[1]) or want
		local masteryName = type(want) == "table" and want.mastery or nil
		local chosen, chosenEffect
		local candidates = {}
		-- exact mastery name if one exists, otherwise substring match
		local exactName = false
		if masteryName then
			for _, node in pairs(spec.nodes) do
				if node.type == "Mastery" and norm(node.dn) == norm(masteryName) then exactName = true break end
			end
		end
		for id, node in pairs(spec.nodes) do
			if node.type == "Mastery" and node.masteryEffects and isRealNode(node)
				and (not masteryName or norm(node.dn) == norm(masteryName) or (not exactName and contains(node.dn, masteryName))) then
				for _, eff in ipairs(node.masteryEffects) do
					local effData = treeObj().masteryEffects[eff.effect]
					local text = effData and t_concat(effData.sd or {}, " / ") or ""
					if contains(text, effectText) and not used[eff.effect] then
						t_insert(candidates, { node = node, effect = eff.effect, text = text })
					end
				end
			end
		end
		-- a pinned entry names its mastery node: take exactly that one
		local wantNode = type(want) == "table" and tonumber(want.node) or nil
		if wantNode then
			for _, c in ipairs(candidates) do
				if c.node.id == wantNode and not spec.masterySelections[c.node.id] then chosen, chosenEffect = c, c.effect break end
			end
		end
		-- otherwise prefer a mastery whose cluster already has an allocated notable (deterministic: lowest node id)
		if not chosen then
			table.sort(candidates, function(a, b) return a.node.id < b.node.id end)
			for _, c in ipairs(candidates) do
				if not spec.masterySelections[c.node.id] and c.node.path and #c.node.path <= 1 then
					chosen, chosenEffect = c, c.effect
					break
				end
			end
		end
		if not chosen then
			if #candidates == 0 then
				err(("Mastery effect '%s'%s not found"):format(effectText, masteryName and (" on " .. masteryName) or ""))
			else
				local names, seenN = {}, {}
				for _, c in ipairs(candidates) do
					if not seenN[c.node.dn] then seenN[c.node.dn] = true t_insert(names, "'" .. c.node.dn .. "' (" .. c.text .. ")") end
				end
				err(("Mastery effect '%s': no matching mastery is next to an allocated notable. Allocate a notable in a cluster with one of: %s")
					:format(effectText, t_concat(names, "; ")))
			end
		else
			spec.masterySelections[chosen.node.id] = chosen.effect
			spec:AllocNode(chosen.node)
			used[chosen.effect] = true
			t_insert(plan, { target = effectText, id = chosen.node.id, name = chosen.node.dn, type = "Mastery", effect = chosen.text, cost = 1 })
		end
	end
end

---------------------------------------------------------------------------
-- Build mode: spec JSON -> PoB XML
---------------------------------------------------------------------------
local SLOT_ALIASES = {
	["weapon"] = "Weapon 1", ["mainhand"] = "Weapon 1", ["weapon1"] = "Weapon 1",
	["offhand"] = "Weapon 2", ["shield"] = "Weapon 2", ["quiver"] = "Weapon 2", ["weapon2"] = "Weapon 2",
	["helm"] = "Helmet", ["helmet"] = "Helmet", ["body"] = "Body Armour", ["bodyarmour"] = "Body Armour", ["chest"] = "Body Armour", ["bodyarmor"] = "Body Armour",
	["gloves"] = "Gloves", ["boots"] = "Boots", ["amulet"] = "Amulet", ["ring1"] = "Ring 1", ["ring2"] = "Ring 2",
	["belt"] = "Belt", ["flask1"] = "Flask 1", ["flask2"] = "Flask 2", ["flask3"] = "Flask 3", ["flask4"] = "Flask 4", ["flask5"] = "Flask 5",
}
local function canonSlot(slot)
	if not slot then return nil end
	local n = norm(slot)
	if SLOT_ALIASES[n] then return SLOT_ALIASES[n] end
	local jid = tostring(slot):match("^[Jj]ewel%s*(%d+)$")
	if jid then return "Jewel " .. jid end
	return slot
end

local function xmlNode(elem, attrib, children)
	local n = { elem = elem, attrib = attrib or {} }
	for _, c in ipairs(children or {}) do t_insert(n, c) end
	return n
end

local function findChild(parent, elem)
	for i, c in ipairs(parent) do
		if type(c) == "table" and c.elem == elem then return c, i end
	end
end

local function replaceChild(root, node)
	local _, idx = findChild(root, node.elem)
	if idx then root[idx] = node else t_insert(root, node) end
end

-- Add Searing Exarch / Eater of Worlds implicits (and their item markers) to raw item text.
local function addEldritch(raw, exarch, eater)
	if not exarch and not eater then return raw end
	local lines = {}
	for l in (raw .. "\n"):gmatch("([^\n]*)\n") do t_insert(lines, l) end
	local function hasLine(s) for _, l in ipairs(lines) do if l == s then return true end end end
	local add = {}
	if exarch then t_insert(add, "{exarch}" .. exarch) end
	if eater then t_insert(add, "{eater}" .. eater) end
	-- influence markers go right after the base-type line
	local baseIdx = lines[1] and lines[1]:match("^Rarity: N") and 2 or 3
	local markers = {}
	if exarch and not hasLine("Searing Exarch Item") then t_insert(markers, "Searing Exarch Item") end
	if eater and not hasLine("Eater of Worlds Item") then t_insert(markers, "Eater of Worlds Item") end
	for i = #markers, 1, -1 do t_insert(lines, baseIdx + 1, markers[i]) end
	local implIdx
	for i, l in ipairs(lines) do if l:match("^Implicits: %d+") then implIdx = i break end end
	if implIdx then
		local n = tonumber(lines[implIdx]:match("%d+"))
		lines[implIdx] = "Implicits: " .. (n + #add)
		for i = #add, 1, -1 do t_insert(lines, implIdx + 1, add[i]) end
	else
		-- No "Implicits:" header: append flagged lines at the end so they don't split the
		-- header block (Item Level, Quality, Sockets...); "(implicit)" marks them as implicits.
		for _, a in ipairs(add) do t_insert(lines, a .. " (implicit)") end
	end
	return t_concat(lines, "\n")
end

-- Replace the item's implicit block with `implLines` (keeps everything else).
local function setImplicitBlock(raw, implLines)
	local lines = {}
	for l in (raw .. "\n"):gmatch("([^\n]*)\n") do t_insert(lines, l) end
	local idx
	for i, l in ipairs(lines) do if l:match("^Implicits: %d+") then idx = i break end end
	if idx then
		-- PoB counts enchant (anoint) and scourge lines in the Implicits block: keep those, replace the rest
		local n = tonumber(lines[idx]:match("%d+"))
		local kept = {}
		for _ = 1, n do
			local l = table.remove(lines, idx + 1)
			if l and (l:find("^{enchant}") or l:find("^{scourge}")) then t_insert(kept, l) end
		end
		local block = {}
		for _, l in ipairs(kept) do t_insert(block, l) end
		for _, l in ipairs(implLines) do t_insert(block, l) end
		lines[idx] = "Implicits: " .. #block
		for i = #block, 1, -1 do t_insert(lines, idx + 1, block[i]) end
	else
		local kept = {}
		for _, l in ipairs(lines) do if not l:match(" %(implicit%)$") then t_insert(kept, l) end end
		lines = kept
		for _, l in ipairs(implLines) do t_insert(lines, l .. " (implicit)") end
	end
	while lines[#lines] == "" do table.remove(lines) end
	return t_concat(lines, "\n")
end

-- Implicits for an item entry of the spec:
--   implicits = {...}           explicit implicit lines (validated against base/unique/corruption pools)
--   corrupted_implicits = {...} corruption implicits replacing the item's implicits (marks it Corrupted)
--   corrupted = true            mark Corrupted (implicits stay unless replaced)
--   implicit_roll = 0..1        roll used for auto-added base implicits (default 0.5, spec-wide or per item)
-- Normal/Magic/Rare items with no implicits get their base implicit automatically, unless they
-- have Exarch/Eater implicits (those replace the base implicit in game).
local autoImplicitNotes = {}
-- tree.fill options from the spec (build mode only): spend the unspent points after the first calculation
local FILL = nil
-- tree.cluster: node names inside socketed cluster jewels to allocate after the items are loaded
local CLUSTER = nil
-- prune redundant travel after targets and after the fill (build mode, not for a pinned tree; tree.prune=false disables)
local PRUNE = false
-- tree.attribute_repair (default true): the points the pathing saved pay for attribute nodes when the tighter
-- route no longer supplies a requirement; false restores the strict policy (any shortfall is an error)
local ATTR_REPAIR = false
-- config keys the spec set itself (build mode): an explicit value, true or false, is never overridden by
-- the inference pass below, so `"useFrenzyCharges": false` is an opt-out
local SPEC_CONFIG = {}
-- spec "infer_config": false switches the inference pass off (assumption tests, floor reconstructions)
local INFER_CONFIG = true
-- spec "legacy_gems": true accepts Awakened gems (Standard league / legacy items)
local LEGACY_GEMS = false
-- stage counts / skill parts written in the build XML (spec or imported), read before PoB loads it
local explicitStages, explicitParts = {}, {}
local function readExplicitSkillSettings(xmlText)
	explicitStages, explicitParts = {}, {}
	for tag in tostring(xmlText or ""):gmatch("<Gem [^>]*>") do
		local name = tag:match('nameSpec="([^"]*)"')
		if name then
			local st = tag:match('skillStageCount="(%d+)"')
			local pt = tag:match('skillPart="(%d+)"')
			if st then explicitStages[name] = tonumber(st) end
			if pt then explicitParts[name] = tonumber(pt) end
		end
	end
end
local function applyImplicits(raw, it, specIn, slot)
	local isUnique = raw:match("^Rarity: UNIQUE") or raw:match("^Rarity: RELIC")
	local markCorrupted = it.corrupted or it.corrupted_implicits ~= nil
	if it.corrupted_implicits or it.implicits then
		raw = setImplicitBlock(raw, it.corrupted_implicits or it.implicits)
	elseif not isUnique and not it.exarch and not it.eater then
		local probe = new("Item", raw)
		if probe.base and probe.base.implicit and #probe.implicitModLines == 0 then
			local roll = tonumber(it.implicit_roll or specIn.implicit_roll) or 0.5
			local list = {}
			for _, l in ipairs(splitLines(probe.base.implicit)) do t_insert(list, ("{range:%s}%s"):format(roll, l)) end
			raw = setImplicitBlock(raw, list)
			t_insert(autoImplicitNotes, ("%s: %s"):format(slot or probe.baseName, probe.base.implicit:gsub("\n", " / ")))
		end
	end
	if markCorrupted and not raw:find("\nCorrupted") then raw = raw .. "\nCorrupted" end
	return raw
end

local FILL_OBJECTIVES = { balanced = { dps = 1, def = 1 }, dps = { dps = 1, def = 0.25 }, defence = { dps = 0.25, def = 1 },
	life = { dps = 0.25, def = 1 } }

local function parseFill(fill)
	if fill == nil or fill == false then return nil end
	local o = type(fill) == "table" and fill or { objective = fill }
	if o.objective == true then o.objective = "balanced" end
	local objective = lc(o.objective or "balanced")
	if not FILL_OBJECTIVES[objective] then
		err(("tree.fill: unknown objective '%s' (balanced, dps, defence)"):format(tostring(o.objective)))
		return nil
	end
	return { objective = objective, weights = FILL_OBJECTIVES[objective],
		reserve = math.max(0, math.floor(tonumber(o.reserve) or 0)),
		max_cost = math.max(1, math.floor(tonumber(o.max_cost) or 5)),
		max_recalcs = math.max(1, math.floor(tonumber(o.max_recalcs) or 400)) }
end

local function buildFromSpec(specIn)
	for i = #autoImplicitNotes, 1, -1 do autoImplicitNotes[i] = nil end   -- per-build state (batch mode)
	FILL = nil
	newBuild()
	local spec = build.spec
	local plan = { tree = {}, ascendancy = {}, masteries = {} }

	-- tree version (default: latest)
	if specIn.tree_version and specIn.tree_version ~= spec.treeVersion then
		note("tree_version is ignored in build mode; using latest tree " .. spec.treeVersion)
	end

	-- class / ascendancy
	local classId, class = findClass(specIn.class or "")
	if not classId then
		err("Unknown class '" .. tostring(specIn.class) .. "' (Marauder, Ranger, Witch, Duelist, Templar, Shadow, Scion)")
		finish()
	end
	spec:SelectClass(classId)
	local ascId, asc, realClass = findAscendancy(class, specIn.ascendancy)
	if not ascId then
		if realClass then
			err(("Ascendancy '%s' belongs to %s, not %s"):format(specIn.ascendancy, realClass, class.name))
		else
			err("Unknown ascendancy '" .. tostring(specIn.ascendancy) .. "'")
		end
		ascId = 0
	end
	spec:SelectAscendClass(ascId)
	if specIn.bloodline and specIn.bloodline ~= "" then
		local bid = findBloodline(specIn.bloodline)
		if bid then spec:SelectSecondaryAscendClass(bid)
		else err("Unknown bloodline / secondary ascendancy '" .. tostring(specIn.bloodline) .. "'") end
	end

	local treeIn = specIn.tree or {}
	-- "allocated": an exact node-id list (as written by `build` into <name>.filled.json), set the way
	-- PoB's own import does: no pathing, so the tree is reproduced exactly. Nodes that end up
	-- disconnected are reported by the connectivity check after PoB loads the build.
	if type(treeIn.allocated) == "table" and #treeIn.allocated > 0 then
		local missing = {}
		for _, id in ipairs(treeIn.allocated) do
			local node = spec.nodes[tonumber(id)]
			if node and node.type ~= "Mastery" then
				node.alloc = true
				spec.allocNodes[node.id] = node
			elseif not node and tonumber(id) < 65536 then
				t_insert(missing, tostring(id))
			end
		end
		spec:BuildAllDependsAndPaths()
		if #missing > 0 then err("tree.allocated: unknown node id(s) " .. t_concat(missing, ", ")) end
		plan.allocated = #treeIn.allocated
	end
	TARGET_IDS = {}
	PATHING = nil
	PINNED = plan.allocated ~= nil   -- a pinned tree is the user's exact tree: no search, no prune, no repair
	allocateTargets(treeIn.nodes or treeIn.targets or treeIn.passives or {}, false, plan.tree)
	plan.pathing = PATHING
	allocateTargets(treeIn.ascendancy or specIn.ascendancy_nodes or {}, true, plan.ascendancy)
	allocateMasteries(treeIn.masteries or {}, plan.masteries)
	-- travel that connects nothing any more (an earlier target's route made redundant by a later one) goes;
	-- a pinned tree (tree.allocated) is the user's exact tree and is never pruned
	if plan.allocated == nil and treeIn.prune ~= false then
		local removed = pruneTravel(spec, protectedSet(spec, TARGET_IDS))
		if #removed > 0 then
			plan.pruned = {}
			for _, n in ipairs(removed) do t_insert(plan.pruned, { id = n.id, name = n.dn }) end
		end
	end
	FILL = (plan.allocated == nil) and parseFill(treeIn.fill) or nil   -- a pinned tree is final: no fill
	PRUNE = plan.allocated == nil and treeIn.prune ~= false
	ATTR_REPAIR = PRUNE and treeIn.attribute_repair ~= false
	CLUSTER = type(treeIn.cluster) == "table" and treeIn.cluster or nil
	resp.plan = plan

	-- Serialise what we have so far, then edit the XML
	local xmlText = build:SaveDB("generated")
	local parsed = common.xml.ParseXML(xmlText)
	local root
	for _, n in ipairs(parsed) do if n.elem == "PathOfBuilding" then root = n end end
	if not root then err("Internal: could not parse PoB XML") finish() end

	-- Build attributes
	local level = tonumber(specIn.level) or 90
	local buildNode = findChild(root, "Build")
	buildNode.attrib.level = tostring(level)
	buildNode.attrib.characterLevelAutoMode = "false"
	buildNode.attrib.className = class.name
	buildNode.attrib.ascendClassName = spec.curAscendClassName or "None"
	-- drop cached PlayerStat children
	for i = #buildNode, 1, -1 do table.remove(buildNode, i) end

	-- Skills
	local socketGroupsBySlot = {}
	local skillSet = xmlNode("SkillSet", { id = "1" })
	local mainIdx
	for gi, group in ipairs(specIn.skills or {}) do
		local slot = canonSlot(group.slot)
		local gemsXml = {}
		local colors = {}
		for _, g in ipairs(group.gems or {}) do
			local gname = type(g) == "table" and g.name or g
			local gemData, emsg = findGem(gname)
			if not gemData then
				local sug = gemSuggestions(gname, 5)
				err(("Gem '%s' not found%s"):format(gname, #sug > 0 and (" (similar: " .. t_concat(sug, ", ") .. ")") or ""))
			else
				local lvl = type(g) == "table" and tonumber(g.level) or gemData.naturalMaxLevel or 20
				local stageAttr, partAttr
				if type(g) == "table" and g.stages ~= nil then
					stageAttr = tostring(math.floor(tonumber(g.stages) or 0))
				end
				if type(g) == "table" and g.skill_part ~= nil then
					local idx = tonumber(g.skill_part)
					if not idx then
						for pi, pn in ipairs(partNames(gemData.grantedEffect)) do
							if norm(pn) == norm(g.skill_part) then idx = pi end
						end
					end
					local count = #partNames(gemData.grantedEffect)
					if not idx or idx < 1 or idx > count then
						err(("Gem '%s': skill_part '%s' isn't one of its PoB parts (%s)"):format(gemData.name, tostring(g.skill_part),
							count > 0 and t_concat(partNames(gemData.grantedEffect), " | ") or "this skill has no selectable parts"))
					else
						partAttr = tostring(idx)
					end
				end
				-- minion_skill: which of the summoned minion's skills PoB calculates (index or name). Herald of Agony's
				-- crawler has Mortar (cooldown), Tail Spike and Cleave; PoB defaults to the first, which for a
				-- cooldown skill is not the sustained figure. The minion list comes from the gem's granted effect.
				local minionSkillAttr, minionAttr
				local ge = gemData.grantedEffect
				local minionList = ge and (ge.minionList or (ge.levels and ge.levels[1] and ge.levels[1].minionList)) or nil
				if type(g) == "table" and g.minion_skill ~= nil then
					local minionId = type(minionList) == "table" and minionList[1] or nil
					local minion = minionId and data.minions and data.minions[minionId]
					if not minion then
						err(("Gem '%s': minion_skill given but this gem summons no minion PoB lists"):format(gemData.name))
					else
						local names = {}
						for i, sid in ipairs(minion.skillList or {}) do
							local sk = data.skills[sid]
							names[i] = sk and sk.name or sid
						end
						local idx = tonumber(g.minion_skill)
						if not idx then for i, n in ipairs(names) do if norm(n) == norm(g.minion_skill) then idx = i end end end
						if not idx or idx < 1 or idx > #names then
							err(("Gem '%s': minion_skill '%s' isn't one of %s's skills (%s)"):format(gemData.name, tostring(g.minion_skill), minion.name, t_concat(names, " | ")))
						else
							minionSkillAttr, minionAttr = tostring(idx), minionId
							note(("%s: calculating %s's '%s' (its skills: %s)"):format(gemData.name, minion.name, names[idx], t_concat(names, " | ")))
						end
					end
				end
				local q = type(g) == "table" and tonumber(g.quality) or 20
				t_insert(gemsXml, xmlNode("Gem", {
					skillMinion = minionAttr, skillMinionCalcs = minionAttr, skillMinionSkill = minionSkillAttr, skillMinionSkillCalcs = minionSkillAttr,
					nameSpec = gemData.name, gemId = gemData.gameId, variantId = gemData.variantId, skillId = gemData.grantedEffectId,
					level = tostring(lvl), quality = tostring(q),
					enabled = (type(g) == "table" and g.enabled == false) and "false" or "true",
					enableGlobal1 = "true", enableGlobal2 = "true", count = tostring(type(g) == "table" and g.count or 1),
					skillStageCount = stageAttr, skillStageCountCalcs = stageAttr, skillPart = partAttr, skillPartCalcs = partAttr,
				}))
				t_insert(colors, gemColorLetter(gemData))
			end
		end
		local attrib = {
			enabled = group.enabled == false and "false" or "true",
			includeInFullDPS = (group.full_dps or group.main) and "true" or "false",
			mainActiveSkill = tostring(group.main_active_skill or 1),
			mainActiveSkillCalcs = tostring(group.main_active_skill or 1),
			label = group.label,
		}
		if slot then attrib.slot = slot end
		t_insert(skillSet, xmlNode("Skill", attrib, gemsXml))
		if slot then
			socketGroupsBySlot[slot] = socketGroupsBySlot[slot] or {}
			t_insert(socketGroupsBySlot[slot], colors)
		end
		if group.main and not mainIdx then mainIdx = gi end
	end
	buildNode.attrib.mainSocketGroup = tostring(mainIdx or 1)
	if not mainIdx and #(specIn.skills or {}) > 0 then
		warn("No skill group marked \"main\": true; using the first group as the main skill")
	end
	replaceChild(root, xmlNode("Skills", { activeSkillSet = "1", defaultGemLevel = "normalMaximum", defaultGemQuality = "0" }, { skillSet }))

	-- Items
	local itemsNode = xmlNode("Items", { activeItemSet = "1", useSecondWeaponSet = "false" })
	local itemSet = xmlNode("ItemSet", { id = "1", useSecondWeaponSet = "false" })
	local jewelSockets, autoJewels = {}, {}
	local itemId = 0
	for _, it in ipairs(specIn.items or {}) do
		local slot = canonSlot(it.slot)
		local raw
		if it.unique then
			local u = uniqueByName(it.unique)
			if not u then
				local sug = uniqueSuggestions(it.unique, 5)
				err(("Unique '%s' not found%s"):format(it.unique, #sug > 0 and (" (similar: " .. t_concat(sug, ", ") .. ")") or ""))
			else
				local item = new("Item", u:BuildRaw())
				if it.variant and item.variantList then
					for vi, vname in ipairs(item.variantList) do
						if contains(vname, it.variant) then item.variant = vi end
					end
				end
				item:NormaliseQuality()
				item:BuildAndParseRaw()
				raw = item:BuildRaw()
			end
		elseif it.text or it.raw then
			raw = (it.text or it.raw):gsub("\r", "")
		elseif it.base then
			raw = "Rarity: NORMAL\n" .. it.base .. "\n" .. it.base
		else
			err("Item for slot '" .. tostring(it.slot) .. "' needs one of: unique, text, base")
		end
		if raw and it.anoint then
			-- Blight anoint on an amulet: "anoint": "<Notable Name>"
			if slot ~= "Amulet" then
				err(("Slot '%s': only amulets can be anointed with a notable"):format(tostring(it.slot)))
			else
				local item = new("Item", raw)
				local line = "Allocates " .. it.anoint
				item.enchantModLines = { { line = line, enchant = true } }
				item:BuildAndParseRaw()
				local ml = item.enchantModLines[1]
				local tree = build and build.spec and build.spec.tree
				local nn = tree and tree.notableMap and tree.notableMap[tostring(it.anoint):lower()]
				if not nn or nn.ascendancyName or not ml or ml.extra then
					err(("Anoint '%s' is not a notable that can be anointed (use the exact notable name)"):format(it.anoint))
				else
					raw = item:BuildRaw()
				end
			end
		end
		if raw then
			raw = applyImplicits(raw, it, specIn, slot)
			raw = addEldritch(raw, it.exarch, it.eater)
			-- Add sockets matching the gems if the item text doesn't specify them
			if slot and socketGroupsBySlot[slot] and not raw:find("\nSockets:") then
				local parts = {}
				for _, colors in ipairs(socketGroupsBySlot[slot]) do
					if #colors > 0 then t_insert(parts, t_concat(colors, "-")) end
				end
				if #parts > 0 then
					local socketLine = "Sockets: " .. t_concat(parts, " ")
					-- insert after the base line (2nd/3rd line); PoB accepts it anywhere in the header
					local first, rest = raw:match("^(Rarity:[^\n]*\n[^\n]*\n?[^\n]*)\n(.*)$")
					if first then raw = first .. "\n" .. socketLine .. "\n" .. rest else raw = raw .. "\n" .. socketLine end
				end
			end
			itemId = itemId + 1
			t_insert(itemsNode, xmlNode("Item", { id = tostring(itemId) }, { raw }))
			if slot then
				local jid = slot:match("^Jewel (%d+)$")
				if jid then
					jewelSockets[tonumber(jid)] = itemId
				elseif slot:lower() == "jewel" then
					t_insert(autoJewels, itemId) -- auto-assign later
				else
					t_insert(itemSet, xmlNode("Slot", { name = slot, itemId = tostring(itemId), active = slot:match("^Flask") and "true" or nil }))
				end
			end
		end
	end
	-- auto-assign unslotted jewels to allocated sockets
	local freeSockets = {}
	for id, node in pairs(spec.allocNodes) do
		if node.type == "Socket" and not jewelSockets[id] and not node.expansionJewel then t_insert(freeSockets, id) end
	end
	table.sort(freeSockets)
	local treeNode = findChild(root, "Tree")
	local specXml = findChild(treeNode, "Spec")
	local socketsXml = findChild(specXml, "Sockets")
	if not socketsXml then socketsXml = xmlNode("Sockets") t_insert(specXml, socketsXml) end
	for _, v in ipairs(autoJewels) do
		local nodeId = table.remove(freeSockets, 1)
		if nodeId then
			jewelSockets[nodeId] = v
		else
			err("More jewels than allocated jewel sockets; add jewel sockets (by node id) to tree.nodes")
		end
	end
	for nodeId, v in pairs(jewelSockets) do
		if not spec.allocNodes[nodeId] then
			err(("Jewel socket %s is not allocated (add it to tree.nodes)"):format(tostring(nodeId)))
		end
		t_insert(socketsXml, xmlNode("Socket", { nodeId = tostring(nodeId), itemId = tostring(v) }))
		t_insert(itemSet, xmlNode("Slot", { name = "Jewel " .. nodeId, itemId = tostring(v) }))
	end
	t_insert(itemsNode, itemSet)
	replaceChild(root, itemsNode)
	if #autoImplicitNotes > 0 then
		note("Added base implicits (roll " .. tostring(specIn.implicit_roll or 0.5) .. "): " .. t_concat(autoImplicitNotes, "; "))
	end

	-- Config
	local cfg = {}
	SPEC_CONFIG = {}
	INFER_CONFIG = specIn.infer_config ~= false
	TRIGGER_EVENTS = tonumber(specIn.trigger_events_per_second)
	LEGACY_GEMS = specIn.legacy_gems == true
	for k, v in pairs(specIn.config or {}) do cfg[k] = v SPEC_CONFIG[k] = true end
	-- bandit and pantheon take PoB's ids; accept the display names too ("The Brine King", "Soul of Solaris") and
	-- reject anything else here, because PoB would otherwise fail to load the build with no useful message
	local function pantheonId(v, major)
		local q = norm(tostring(v)):gsub("^soulof", "")
		for id, god in pairs(data.pantheons or {}) do
			if norm(id) == q or norm(god.name or ""):gsub("^soulof", "") == q then
				if (god.isMajorGod and true or false) == major then return id end
				err(("Pantheon '%s' is a %s god, given as %s"):format(tostring(v), god.isMajorGod and "major" or "minor", major and "major" or "minor"))
				return nil
			end
		end
		err(("Unknown %s pantheon '%s' (use the god's name, e.g. Solaris, Lunaris, Arakaali, The Brine King / Abberath, Ralakesh, Tukohama, ...)"):format(major and "major" or "minor", tostring(v)))
	end
	if specIn.bandit then
		local b = norm(tostring(specIn.bandit))
		local ids = { alira = "Alira", kraityn = "Kraityn", oak = "Oak", eramir = "None", none = "None", killall = "None" }
		if ids[b] then cfg.bandit = ids[b] else err(("Unknown bandit '%s' (Alira, Kraityn, Oak, or None/Eramir)"):format(tostring(specIn.bandit))) end
	end
	if specIn.pantheon_major then cfg.pantheonMajorGod = pantheonId(specIn.pantheon_major, true) end
	if specIn.pantheon_minor then cfg.pantheonMinorGod = pantheonId(specIn.pantheon_minor, false) end
	local configSet = xmlNode("ConfigSet", { id = "1", title = "Default" })
	local known = build.configTab.varControls or {}
	for k, v in pairs(cfg) do
		if not known[k] then
			warn("Unknown config option '" .. k .. "' (run the config command to list options)")
		end
		local a = { name = k }
		if type(v) == "boolean" then a.boolean = tostring(v)
		elseif type(v) == "number" then a.number = tostring(v)
		else a.string = tostring(v) end
		t_insert(configSet, xmlNode("Input", a))
	end
	replaceChild(root, xmlNode("Config", { activeConfigSet = "1" }, { configSet }))

	local composed, cerr = common.xml.ComposeXML(root)
	if not composed then err("Internal: XML compose failed: " .. tostring(cerr)) finish() end
	return composed, level
end

---------------------------------------------------------------------------
-- Load XML into PoB, compute, validate and report
---------------------------------------------------------------------------
local function parseSpecNodes(xmlText)
	local parsed = common.xml.ParseXML(xmlText)
	local root
	for _, n in ipairs(parsed or {}) do if n.elem == "PathOfBuilding" then root = n end end
	if not root then return nil end
	local tree = findChild(root, "Tree")
	if not tree then return nil end
	local active = tonumber(tree.attrib.activeSpec) or 1
	local count, specNode = 0, nil
	for _, c in ipairs(tree) do
		if c.elem == "Spec" then
			count = count + 1
			if count == active then specNode = c end
		end
	end
	if not specNode or not specNode.attrib.nodes then return nil end
	local ids = {}
	for id in specNode.attrib.nodes:gmatch("%d+") do ids[tonumber(id)] = true end
	return ids
end

local STAT_KEYS = {
	offence = { "CombinedDPS", "TotalDPS", "TotalDot", "TotalDotDPS", "FullDPS", "FullDotDPS", "AverageHit", "AverageDamage", "Speed", "HitChance",
		"CritChance", "CritMultiplier", "BleedDPS", "IgniteDPS", "PoisonDPS", "ImpaleDPS", "ManaCost", "LifeCost", "ManaPerSecondCost", "AreaOfEffectRadiusMetres" },
	defence = { "Life", "LifeUnreserved", "LifeRegenRecovery", "EnergyShield", "EnergyShieldRegenRecovery", "Mana", "ManaUnreserved", "ManaRegenRecovery", "Ward",
		"Armour", "PhysicalDamageReduction", "Evasion", "MeleeEvadeChance", "ProjectileEvadeChance", "EffectiveBlockChance", "EffectiveSpellBlockChance", "EffectiveSpellSuppressionChance",
		"FireResist", "ColdResist", "LightningResist", "ChaosResist", "FireResistOverCap", "ColdResistOverCap", "LightningResistOverCap", "ChaosResistOverCap",
		"TotalEHP", "PhysicalMaximumHitTaken", "FireMaximumHitTaken", "ColdMaximumHitTaken", "LightningMaximumHitTaken", "ChaosMaximumHitTaken",
		"EffectiveMovementSpeedMod", "StunThreshold", "StunAvoidChance", "StunDuration", "LifeRecoup", "LifeRecoupRecoveryAvg", "EnergyShieldRecoupRecoveryAvg", "ManaRecoupRecoveryAvg" },
	attributes = { "Str", "Dex", "Int", "ReqStr", "ReqDex", "ReqInt" },
}

local function collectStats(out)
	local stats = {}
	for section, keys in pairs(STAT_KEYS) do
		stats[section] = {}
		for _, k in ipairs(keys) do
			local v = out[k]
			if type(v) == "number" and v == v and v ~= math.huge and v ~= -math.huge then
				stats[section][k] = round(v, 2)
			end
		end
	end
	return stats
end

local function sidebarLines()
	local lines = {}
	local list = build.controls and build.controls.statBox and build.controls.statBox.list or {}
	for _, entry in ipairs(list) do
		if entry[1] then
			local l = trim(stripColor(entry[1]))
			local r = entry[2] and trim(stripColor(entry[2])) or ""
			if l ~= "" then t_insert(lines, (r ~= "" and (l .. " " .. r)) or l) end
		end
	end
	return lines
end

-- Value of a gem stat at the gem's level (raw PoB level data, positional like the stats list)
local function gemStatAt(ge, statName, level)
	if not (ge and ge.stats and ge.levels) then return nil end
	for i, name in ipairs(ge.stats) do
		if name == statName then
			local lv = ge.levels[level] or ge.levels[#ge.levels]
			return lv and tonumber(lv[i])
		end
	end
end
local TRIGGER_LIMIT_STAT = "local_support_gem_max_skill_level_requirement_to_support"

local function reportSkills(level)
	local groups = {}
	local itemsTab = build.itemsTab
	for gi, sg in ipairs(build.skillsTab.socketGroupList) do
		local hasGem = false
		for _, gem in ipairs(sg.gemList) do if trim(gem.nameSpec) ~= "" then hasGem = true end end
		-- skip empty groups and ones PoB generates itself (e.g. "On Kill Monster Explosion")
		if not hasGem or sg.source then goto continue end
		local g = { index = gi, slot = sg.slot, enabled = sg.enabled, main = (gi == build.mainSocketGroup), gems = {}, problems = {} }
		-- supports such as Cast when Damage Taken only work for skills up to a level requirement
		for _, sup in ipairs(sg.gemList) do
			local sd = sup.gemData
			local lim = sd and sd.grantedEffect and sd.grantedEffect.support and gemStatAt(sd.grantedEffect, TRIGGER_LIMIT_STAT, sup.level)
			if lim then
				for _, act in ipairs(sg.gemList) do
					local ad = act.gemData
					if ad and ad.grantedEffect and not ad.grantedEffect.support and act.enabled ~= false then
						local lv = ad.grantedEffect.levels and ad.grantedEffect.levels[act.level]
						local req = lv and lv.levelRequirement
						if req and req > lim then
							g.trigger_problems = g.trigger_problems or {}
							t_insert(g.trigger_problems, ("%s level %d (requires level %d) is above what %s level %d supports (requirement up to %d): "
								.. "it won't be triggered in game, though PoB still counts it"):format(act.nameSpec, act.level, req, sup.nameSpec,
								sup.level, lim))
						end
					end
				end
			end
		end
		local seen = {}
		local activeCount, supportCount = 0, 0
		for _, gem in ipairs(sg.gemList) do
			local gd = gem.gemData
			-- Awakened gems: drop-disabled since 3.29, legacy items in Standard only. In build mode the preflight
			-- already repaired or rejected them; an imported build gets a warning.
			if gd and gd.tags and gd.tags.awakened and gem.enabled ~= false and sg.enabled and not LEGACY_GEMS then
				(resp.mode == "check" and warn or err)(("%s: Awakened gems are not obtainable in 3.29+ leagues (drop-disabled; legacy in Standard). Use %s%s"):format(
					gem.nameSpec, gem.nameSpec:gsub("^Awakened ", ""),
					build.data.gems and (function() for _, g2 in pairs(build.data.gems) do if g2.name == "Greater " .. gem.nameSpec:gsub("^Awakened ", "") then return " or " .. g2.name end end return "" end)() or ""))
			end
			local entry = { name = gem.nameSpec, level = gem.level, quality = gem.quality, enabled = gem.enabled ~= false,
				gem_name = gem.gemData and gem.gemData.name or nil, variant_id = gem.gemData and gem.gemData.variantId or nil,
				game_id = gem.gemData and gem.gemData.gameId or nil }
			if not gd and not gem.grantedEffect then
				t_insert(g.problems, ("'%s': %s"):format(gem.nameSpec, gem.errMsg or "unknown gem"))
			elseif gd then
				local ge = gd.grantedEffect
				entry.support = ge.support or false
				entry.color = gemColorLetter(gd)
				if ge.support then supportCount = supportCount + 1 else activeCount = activeCount + 1 end
				local maxLvl = gd.naturalMaxLevel or 20
				if gem.level > maxLvl + 1 then
					t_insert(g.problems, ("%s level %d is above the obtainable maximum (%d, %d with corruption)"):format(gem.nameSpec, gem.level, maxLvl, maxLvl + 1))
				elseif gem.level == maxLvl + 1 then
					entry.note = "needs corruption (Vaal orb) for +1 level"
				end
				if gem.quality > 23 then
					t_insert(g.problems, ("%s quality %d is impossible (max 23 with corruption)"):format(gem.nameSpec, gem.quality))
				elseif gem.quality > 20 then
					entry.note = (entry.note and entry.note .. "; " or "") .. "quality above 20 needs corruption"
				end
				if gem.reqLevel and gem.reqLevel > level then
					t_insert(g.problems, ("%s level %d requires character level %d (build is %d)"):format(gem.nameSpec, gem.level, gem.reqLevel, level))
				end
				if ge.support and seen[gd.name] then
					t_insert(g.problems, ("Duplicate support '%s' in the same link (only one applies)"):format(gd.name))
				end
				seen[gd.name] = true
				-- does this support apply to any active skill in the group?
				if ge.support and sg.enabled and gem.enabled ~= false then
					local de = gem.displayEffect
					if de and de.superseded then
						entry.note = (entry.note and entry.note .. "; " or "") .. "superseded by a stronger copy"
					elseif sg.displaySkillList and #sg.displaySkillList > 0 and (not de or not de.isSupporting or not next(de.isSupporting)) then
						t_insert(g.problems, ("Support '%s' can't apply to any active skill in this group"):format(gd.name))
					end
				end
				if gem.matchesSocket == false and sg.slot then
					entry.socket_color_match = false
				end
			end
			t_insert(g.gems, entry)
		end
		-- links vs item sockets
		if sg.slot and itemsTab.slots[sg.slot] then
			local item = itemsTab.items[itemsTab.slots[sg.slot].selItemId]
			if not item then
				t_insert(g.problems, "Gems are socketed in '" .. sg.slot .. "' but no item is equipped there")
			elseif item.sockets and #item.sockets > 0 then
				local groupsSize = {}
				for _, s in ipairs(item.sockets) do
					if s.color ~= "A" then groupsSize[s.group] = (groupsSize[s.group] or 0) + 1 end
				end
				local largest = 0
				for _, n in pairs(groupsSize) do largest = math.max(largest, n) end
				local nGems = 0
				for _, gem in ipairs(sg.gemList) do if gem.gemData then nGems = nGems + 1 end end
				g.link_size = largest
				if nGems > largest then
					t_insert(g.problems, ("%d gems but the largest link on %s is %d"):format(nGems, item.title or item.name, largest))
				end
				-- off-colour sockets
				local mismatch = 0
				for _, gem in ipairs(sg.gemList) do if gem.gemData and gem.matchesSocket == false then mismatch = mismatch + 1 end end
				if mismatch > 0 then
					g.socket_color_mismatches = mismatch
				end
			end
		end
		t_insert(groups, g)
		::continue::
	end
	return groups
end

---------------------------------------------------------------------------
-- Attributes: have / need / margin per attribute, what sets the need, and on a shortfall (or a
-- thin margin) the ways to meet it, cheapest first: gem level, amulet base, a free suffix on an
-- equipped rare or jewel, attribute nodes next to the tree. Fill points are never the answer.
---------------------------------------------------------------------------
local ATTRS = { "Str", "Dex", "Int" }
local ATTR_LONG = { Str = "Strength", Dex = "Dexterity", Int = "Intelligence" }
local ATTR_MARGIN = 10

local function attrOfMod(mod)
	-- which attribute(s) a node/item modifier grants, as PoB parsed it
	if mod.name == "Str" or mod.name == "Dex" or mod.name == "Int" then return { [mod.name] = true } end
	if mod.name == "All" or mod.name == "AllAttributes" then return { Str = true, Dex = true, Int = true } end
	return nil
end

local function gemNeedAt(gd, l, a)
	local ge = gd.grantedEffect
	local lv = ge and ge.levels and ge.levels[l]
	if not lv then return nil end
	return calcLib.getGemStatRequirement(lv.levelRequirement or 1, ge.support, gd["req" .. a] or 0)
end

local function attributeReport(level, itemsReport)
	local itemsBySlot = {}
	for _, r in ipairs(itemsReport or {}) do itemsBySlot[r.slot] = r end
	local o = build.calcsTab.mainOutput
	local env = build.calcsTab.mainEnv
	local rep = { margin_target = ATTR_MARGIN, attributes = {}, shortfall = {}, thin_margin = {}, advice = {} }
	for _, a in ipairs(ATTRS) do
		local have, need = math.floor(o[a] or 0), math.floor(o["Req" .. a] or 0)
		local setBy = {}
		for _, r in ipairs(env.requirementsTable or {}) do
			local v = tonumber(r[a]) or 0
			if v > 0 and v >= need then
				if r.source == "Gem" and r.sourceGem then
					t_insert(setBy, { kind = "gem", name = r.sourceGem.nameSpec or (r.sourceGem.gemData and r.sourceGem.gemData.name) or "?",
						level = r.sourceGem.level, need = v, gem = r.sourceGem })
				elseif r.source == "Item" and r.sourceItem then
					t_insert(setBy, { kind = "item", name = r.sourceItem.title or r.sourceItem.name or "?", slot = r.sourceSlot, need = v })
				end
			end
		end
		table.sort(setBy, function(x, y) return x.name < y.name end)
		local entry = { have = have, need = need, margin = have - need, set_by = {} }
		for _, sb in ipairs(setBy) do
			t_insert(entry.set_by, sb.kind == "gem" and ("%s %d"):format(sb.name, sb.level or 0) or ("%s (%s)"):format(sb.name, sb.slot or "?"))
		end
		rep.attributes[a] = entry
		if need > have then rep.shortfall[a] = need - have
		elseif need > 0 and have - need < ATTR_MARGIN then rep.thin_margin[a] = have - need end
		entry._set_by = setBy
	end

	-- advice for every attribute short or thin; each option says what it costs (displaces)
	for _, a in ipairs(ATTRS) do
		local gap = rep.shortfall[a] or (rep.thin_margin[a] and (ATTR_MARGIN - rep.thin_margin[a])) or nil
		if gap then
			local entry = rep.attributes[a]
			local have = entry.have
			local opts = {}
			-- 1) lower the driving gem's level until the requirement fits (compare measures the DPS cost)
			for _, sb in ipairs(entry._set_by) do
				if sb.kind == "gem" and sb.gem.gemData and sb.gem.level and sb.gem.level > 1 then
					local gd = sb.gem.gemData
					local fit, fitNeed
					for l = sb.gem.level - 1, 1, -1 do
						local n = gemNeedAt(gd, l, a)
						if n and n + ATTR_MARGIN <= have then fit, fitNeed = l, n break end
					end
					local oneLess = gemNeedAt(gd, sb.gem.level - 1, a)
					t_insert(opts, { order = 1, kind = "gem_level", gem = sb.name, from_level = sb.gem.level,
						to_level = fit, need_at_fit = fitNeed, need_one_level_lower = oneLess,
						cost = "gem level (DPS/effect; measure with compare)",
						text = fit and ("%s level %d -> %d: need %d -> %d"):format(sb.name, sb.gem.level, fit, sb.need, fitNeed)
							or ("%s level %d -> %d: need %d -> %d (not enough alone)"):format(sb.name, sb.gem.level, sb.gem.level - 1, sb.need, oneLess or sb.need) })
				end
			end
			-- 2) amulet base with the attribute as implicit (free: no affix, no point)
			local itemsTab = build.itemsTab
			local amSlot = itemsTab.slots["Amulet"]
			local amulet = amSlot and itemsTab.items[amSlot.selItemId]
			if not amulet or amulet.rarity ~= "UNIQUE" then
				local curImp = amulet and amulet.base and amulet.base.implicit or ""
				local givesAlready = curImp:find(ATTR_LONG[a], 1, true) or curImp:find("all Attributes", 1, true)
				if not givesAlready then
					local bases = {}
					for name, base in pairs(build.data.itemBases) do
						if base.type == "Amulet" and base.implicit and not (base.hidden) and not name:find("Talisman")
							and (base.implicit:find(ATTR_LONG[a], 1, true) or base.implicit:find("all Attributes", 1, true)) then
							t_insert(bases, name .. " (" .. base.implicit .. ")")
						end
					end
					table.sort(bases)
					if #bases > 0 then
						t_insert(opts, { order = 2, kind = "amulet_base", bases = bases,
							cost = amulet and ("replaces the amulet's current implicit (" .. (curImp ~= "" and curImp or "none") .. ")") or "no amulet equipped: free",
							text = "amulet base: " .. t_concat(bases, ", ") })
					end
				end
			end
			-- 3) a free suffix on an equipped rare/magic item or jewel, with the best tier its item level allows
			local slots = {}
			for slotName, slot in pairs(itemsTab.slots) do
				local item = itemsTab.items[slot.selItemId]
				if item and item.base and (not slot.nodeId or build.spec.allocNodes[slot.nodeId])
					and (item.rarity == "RARE" or item.rarity == "MAGIC") and not item.corrupted and not item.mirrored then
					local maxS = item.rarity == "MAGIC" and 1 or (item.base.type == "Jewel" and 2 or 3)
					local ir = itemsBySlot[slotName]
					local used = ir and ir.suffixes or maxS   -- as counted by the item check; unknown = assume full
					if used < maxS then
						local best, bestName
						local ilvl = item.itemLevel or 86
						for _, mod in pairs(item.affixes or build.data.itemMods.Item) do
							if mod.type == "Suffix" and (mod.group == ATTR_LONG[a] or mod.group == "AllAttributes")
								and (mod.level or 1) <= ilvl and item:GetModSpawnWeight(mod) > 0 and not (mod.affix or ""):find("Essence") then
								local _, ranges = templateToPattern(mod[1] or "")
								local top = ranges[1] and math.max(ranges[1][1], ranges[1][2]) or tonumber((mod[1] or ""):match("%d+")) or 0
								if top > (best or 0) then best, bestName = top, (mod[1] or "") end
							end
						end
						if best then
							t_insert(slots, { slot = slotName, item = item.title or item.name, suffixes_used = used, suffixes_max = maxS,
								best = best, mod = bestName, item_level = ilvl })
						end
					end
				end
			end
			table.sort(slots, function(x, y) if x.best ~= y.best then return x.best > y.best end return x.slot < y.slot end)
			if #slots == 0 then
				t_insert(opts, { order = 3, kind = "suffix_none", cost = "-",
					text = "no equipped rare, magic item or jewel has a free suffix that can roll " .. ATTR_LONG[a]
						.. " (free a suffix, or choose a base that can roll it)" })
			end
			for i, sl in ipairs(slots) do
				if i > 4 then break end
				t_insert(opts, { order = 3, kind = "suffix", slot = sl.slot, item = sl.item, suffixes_used = sl.suffixes_used,
					suffixes_max = sl.suffixes_max, best = sl.best, mod = sl.mod,
					cost = ("one suffix on %s (%d/%d used), which could hold resistance instead"):format(sl.slot, sl.suffixes_used, sl.suffixes_max),
					text = ("%s (%s, ilvl %d, suffixes %d/%d): %s"):format(sl.slot, sl.item, sl.item_level, sl.suffixes_used, sl.suffixes_max, sl.mod) })
			end
			-- 4) attribute nodes within two points of the allocated tree (1 point for +30 beats a T1 suffix when suffixes are scarce)
			local nodes = {}
			for _, node in pairs(build.spec.nodes) do
				if not node.alloc and node.path and #node.path >= 1 and #node.path <= 2 and not node.ascendancyName and not node.isProxy
					and (node.type == "Normal" or node.type == "Notable") and node.modList then
					local gain = 0
					for _, mod in ipairs(node.modList) do
						local which = attrOfMod(mod)
						if which and which[a] and mod.type == "BASE" then gain = gain + (tonumber(mod.value) or 0) end
					end
					local cost = 0
					for _, p in ipairs(node.path) do if not p.alloc then cost = cost + 1 end end
					if gain >= 10 and cost >= 1 then
						t_insert(nodes, { name = node.dn, id = node.id, gain = gain, cost = cost, per_point = gain / cost })
					end
				end
			end
			table.sort(nodes, function(x, y) if x.per_point ~= y.per_point then return x.per_point > y.per_point end
				if x.name ~= y.name then return x.name < y.name end return x.id < y.id end)
			for i, n in ipairs(nodes) do
				if i > 4 then break end
				t_insert(opts, { order = 4, kind = "node", name = n.name, id = n.id, gain = n.gain, points = n.cost,
					cost = n.cost .. " passive point" .. (n.cost > 1 and "s" or "") .. " (not fill points: name the node in tree.nodes)",
					text = ("%s (id %d): +%d %s for %d point%s"):format(n.name, n.id, n.gain, a, n.cost, n.cost > 1 and "s" or "") })
			end
			table.sort(opts, function(x, y) return x.order < y.order end)
			rep.advice[a] = { gap = gap, short = rep.shortfall[a] ~= nil, options = opts }
		end
	end
	for _, a in ipairs(ATTRS) do rep.attributes[a]._set_by = nil end
	return rep
end

local function reportItems(level)
	local out = {}
	local itemsTab = build.itemsTab
	local used = {}
	for slotName, slot in pairs(itemsTab.slots) do
		local item = itemsTab.items[slot.selItemId]
		if item and (not slot.nodeId or build.spec.allocNodes[slot.nodeId]) then
			used[item.id] = true
			local r = checkItemMods(item, slotName)
			local req = item.requirements and item.requirements.level
			if req and req > level then
				t_insert(r.problems, ("Requires level %d (build is %d)"):format(req, level))
			end
			if item.sockets and item.base and item.base.socketLimit then
				local n = 0
				for _, s in ipairs(item.sockets) do if s.color ~= "A" then n = n + 1 end end
				if n > item.base.socketLimit and item.rarity ~= "UNIQUE" then
					t_insert(r.problems, ("%d sockets but %s allows at most %d"):format(n, item.baseName, item.base.socketLimit))
				end
				-- chromatic difficulty note
				if n > 0 and item.base.req then
					local off = 0
					for _, s in ipairs(item.sockets) do
						if (s.color == "R" and not item.base.req.str) or (s.color == "G" and not item.base.req.dex) or (s.color == "B" and not item.base.req.int) then
							off = off + 1
						end
					end
					if off >= 4 then
						t_insert(r.notes, off .. " off-colour sockets: expensive to colour (consider Vorici bench / a different base)")
					end
				end
			end
			if #r.problems > 0 or #r.notes > 0 or true then t_insert(out, r) end
		end
	end
	for id, item in pairs(itemsTab.items) do
		if not used[id] then
			warn(("Item '%s' is not equipped in any slot"):format(item.title or item.name or tostring(id)))
		end
	end
	table.sort(out, function(a, b) return tostring(a.slot) < tostring(b.slot) end)
	return out
end

---------------------------------------------------------------------------
-- Quality audits (request flag "audit"). Everything is measured with PoB itself:
-- toggle one thing off, recalculate, compare, restore. Nothing is estimated here.
---------------------------------------------------------------------------
local AUDIT = false
local REFERENCE = false   -- extra measurements for reference-build analysis (request flag "reference")
local Cand = {}           -- candidate discovery and the build profile (functions defined before analyse)

-- Defensive / recovery metrics PoB already computes (reported raw; thresholds are applied in pob.py)
local DEFENCE_KEYS = {
	"Life", "LifeUnreserved", "EnergyShield", "Mana", "ManaUnreserved", "Ward", "TotalEHP", "EHPSurvivalTime",
	"PhysicalMaximumHitTaken", "FireMaximumHitTaken", "ColdMaximumHitTaken", "LightningMaximumHitTaken", "ChaosMaximumHitTaken",
	"PhysicalDamageReduction", "Armour", "Evasion", "MeleeEvadeChance", "ProjectileEvadeChance",
	"EffectiveBlockChance", "EffectiveSpellBlockChance", "EffectiveSpellSuppressionChance", "SpellSuppressionEffect",
	"AvoidPhysicalDamageChance", "AvoidFireDamageChance", "AvoidColdDamageChance", "AvoidLightningDamageChance", "AvoidChaosDamageChance",
	"FireResist", "ColdResist", "LightningResist", "ChaosResist", "FireResistOver75", "ColdResistOver75", "LightningResistOver75",
	"LifeRegenRecovery", "LifeRegenPercent", "NetLifeRegen", "LifeLeechGainRate", "MaxLifeLeechRate", "LifeRecoup",
	"PhysicalLifeRecoup", "FireLifeRecoup", "ColdLifeRecoup", "LightningLifeRecoup", "ChaosLifeRecoup", "LifeFlaskRecovery",
	"EnergyShieldRegenRecovery", "EnergyShieldLeechGainRate", "EnergyShieldRecoup", "NetEnergyShieldRegen", "EnergyShieldRecharge", "EnergyShieldRechargeDelay", "ManaRegenRecovery", "NetManaRegen",
	"sharedMindOverMatter", "sharedGuardAbsorb", "sharedAegis", "sharedElementalAegis",
	"FreezeAvoidChance", "ChillAvoidChance", "ShockAvoidChance", "IgniteAvoidChance", "BleedAvoidChance", "PoisonAvoidChance",
	"StunAvoidChance", "CurseEffectOnSelf", "CurseAvoidChance", "EffectiveMovementSpeedMod",
}

local PERMANENT_CONFIG = { bandit = true, pantheonMajorGod = true, pantheonMinorGod = true, enemyLevel = true, enemyIsBoss = true,
	presetBossSkills = true, resistancePenalty = true }

local function configCategory(var)
	if PERMANENT_CONFIG[var] then return "permanent" end
	local v = var:lower()
	if v:find("charges") then return "charges" end
	if v:find("kill") then return "kill condition" end
	if v:find("^conditionenemy") or v:find("^enemy") or v:find("enemy") then return "enemy condition" end
	if v:find("^buff") or v:find("onslaught") or v:find("fortif") or v:find("tailwind") or v:find("adrenaline") then return "buff" end
	if v:find("^condition") then return "player condition" end
	if v:find("^multiplier") then return "stacks/uptime" end
	return "other"
end

---------------------------------------------------------------------------
-- Evidence for conditional configuration (does the build have a source for it?)
---------------------------------------------------------------------------
-- PoB applies a conditional option for 100% of the time; it doesn't model whether the build can
-- generate the condition or how often. This collects evidence only: PoB-calculated chances, PoB's own
-- option gates and capability flags, and source lines in the build's allocated passives, equipped
-- gear (active set) and enabled gems. It never estimates uptime.
local CONDITION_KEYWORDS = {
	buffFortification = { "%f[%a]fortif" }, buffOnslaught = { "%f[%a]onslaught" }, buffTailwind = { "%f[%a]tailwind" },
	buffAdrenaline = { "%f[%a]adrenaline" }, buffUnholyMight = { "unholy might" }, buffPhasing = { "%f[%a]phasing" },
	buffElusive = { "%f[%a]elusive" }, multiplierRage = { "%f[%a]rage%f[%A]" },
	conditionEnemyBlinded = { "%f[%a]blind" }, conditionEnemyOnScorchedGround = { "scorched ground" },
	conditionEnemyShocked = { "%f[%a]shock" }, conditionEnemyChilled = { "%f[%a]chill" }, conditionEnemyIgnited = { "%f[%a]ignit" },
	conditionEnemyFrozen = { "%f[%a]freez", "%f[%a]frozen" }, conditionEnemyIntimidated = { "%f[%a]intimidat" },
	conditionEnemyUnnerved = { "%f[%a]unnerv" }, conditionEnemyMaimed = { "%f[%a]maim" }, conditionEnemyHindered = { "%f[%a]hinder" },
	conditionEnemyBleeding = { "%f[%a]bleed" }, conditionEnemyPoisoned = { "%f[%a]poison" }, conditionEnemyCoveredInAsh = { "covered in ash" },
	conditionEnemyBrittle = { "%f[%a]brittle" }, conditionEnemySapped = { "%f[%a]sap%f[%A]", "%f[%a]sapped" },
	conditionEnemyScorched = { "%f[%a]scorch" }, conditionEnemyCursed = { "%f[%a]curse" }, conditionEnemyTaunted = { "%f[%a]taunt" },
	configResonanceCount = { "%f[%a]resonance" }, bloodSandStance = { "%f[%a]stance" },
	multiplierWitheredStackCount = { "%f[%a]wither" }, multiplierMineDetonatedRecently = { "high%-impact mine" },
	heraldOfAgonyVirulenceStack = { "%f[%a]virulence" },
}
-- the main skill's PoB-calculated chance to inflict an enemy condition, per hit
local CONDITION_CHANCE = { conditionEnemyShocked = { "ShockChance", "Shock" }, conditionEnemyIgnited = { "IgniteChance", "Ignite" },
	conditionEnemyFrozen = { "FreezeChance", "Freeze" }, conditionEnemyBrittle = { "BrittleChance", "inflict Brittle" },
	conditionEnemySapped = { "SapChance", "Sap" }, conditionEnemyScorched = { "ScorchChance", "Scorch" },
	conditionEnemyPoisoned = { "PoisonChance", "Poison" }, conditionEnemyBleeding = { "BleedChance", "cause Bleeding" } }
-- a keyword right after one of these words is used, not generated ("per Frenzy Charge", "against Blinded")
local CONSUMER_BEFORE = { "per%s+$", "against%s+$", "while%s+$", "while you have%s+$", "while you have an?%s+$", "if you have%s+$",
	"for each%s+$", "maximum%s+$", "effect of%s+$", "with%s+$", "on%s+$", "from%s+$", "by%s+$", "if%s+$", "when%s+$",
	"immunity to%s+$", "immune to%s+$", "cannot be%s+$", "removes?%s+$", "avoid%s+$" }
local SOURCE_WORDS = { "gain", "grant", "give", "chance to", "inflict", "steal", "drops", "creat", "leav", "appl", "caus", "minimum",
	"enemies are", "always", "trigger", "you are " }

-- support gems whose PoB description says nothing about what they generate: known by name
GEM_NAME_SOURCES = {
	["charged mines"] = { "usePowerCharges", "useFrenzyCharges" },   -- on detonation: sustaining for a mine build
	["charged traps"] = { "usePowerCharges", "useFrenzyCharges" },
	["power charge on critical"] = { "usePowerCharges" },
	["onslaught"] = { "buffOnslaught" },
	["innervate"] = { "conditionEnemyShocked" },                        -- on kill: judged by triggerOf
}
-- gem names that contain a condition's keyword without generating it ("Blood Rage" is a buff, not a Rage source;
-- its frenzy charge is on kill and judged from the description, not the name)
local GEM_NAME_NOT_SOURCE = { multiplierRage = { "blood rage" } }

local function keywordsFor(var)
	if CONDITION_KEYWORDS[var] then return CONDITION_KEYWORDS[var] end
	local charge = var:match("^use(%u%a-)Charges$")
	if charge then return { { "%f[%a]" .. charge:lower() .. "%f[%A]", "charge" } } end
end

local function buildSourceLines()
	local out = {}
	local function add(src, text, isGemName) if text and text ~= "" then t_insert(out, { src = src, text = stripColor(text), name = isGemName }) end end
	local spec = build.spec
	for id, node in pairs(spec.allocNodes) do
		if node.type == "Mastery" then
			local eff = spec.masterySelections[id] and treeObj().masteryEffects[spec.masterySelections[id]]
			for _, l in ipairs(eff and eff.sd or {}) do add("passive " .. node.dn, l) end
		else
			for _, l in ipairs(node.sd or {}) do add("passive " .. node.dn, l) end
		end
	end
	for name, slot in pairs(build.itemsTab.slots) do
		local item = slot.selItemId and slot.selItemId > 0 and build.itemsTab.items[slot.selItemId]
		local live = item and not name:match("Swap") and (not name:match("^Flask") or slot.active)
			and (not slot.nodeId or spec.allocNodes[slot.nodeId])
		if live then
			for _, list in ipairs({ item.implicitModLines, item.explicitModLines, item.enchantModLines, item.scourgeModLines,
				item.crucibleModLines }) do
				for _, ml in ipairs(list or {}) do
					if not (ml.variantList and item.variant) or ml.variantList[item.variant] then
						add(name .. " " .. (item.title or item.name or "?"), rolledLine(ml))
					end
				end
			end
		end
	end
	for _, sg in ipairs(build.skillsTab.socketGroupList) do
		if sg.enabled then
			for _, gem in ipairs(sg.gemList) do
				local ge = gem.gemData and gem.gemData.grantedEffect
				if ge and gem.enabled ~= false then
					add("gem " .. (gem.nameSpec or ge.name), gem.nameSpec or ge.name, true)
					-- a support's description says what it supports, not what it generates
					if not ge.support then add("gem " .. (gem.nameSpec or ge.name), ge.description) end
				end
			end
		end
	end
	return out
end

local function lineIsSource(text, kw)
	local lower = text:lower()
	local pat, also = kw, nil
	if type(kw) == "table" then pat, also = kw[1], kw[2] end
	local s0 = lower:find(pat)
	if not s0 or (also and not lower:find(also, 1, true)) then return false end
	local before = lower:sub(math.max(1, s0 - 24), s0 - 1)
	for _, c in ipairs(CONSUMER_BEFORE) do if before:find(c) then return false end end
	for _, w in ipairs(SOURCE_WORDS) do if lower:find(w, 1, true) then return true end end
	return false
end

local function enabledGemNames()
	local names = {}
	for _, sg in ipairs(build.skillsTab.socketGroupList) do
		if sg.enabled then
			for _, gem in ipairs(sg.gemList) do
				local ge = gem.gemData and gem.gemData.grantedEffect
				if ge and gem.enabled ~= false then names[ge.name] = true names[gem.nameSpec or ""] = true end
			end
		end
	end
	return names
end

-- How a source triggers, from its own text. "sustaining" types can plausibly keep a condition up while
-- the build attacks or casts; the others can't be assumed to (kills are rare against one boss, a
-- minimum count is only that count, movement stops while attacking). A label, not an uptime.
local function triggerOf(kind, text, var)
	local t = text:lower()
	if kind == "PoB option" or kind == "PoB flag" then return "build mechanic", true end
	if kind == "PoB calculation" then return "per hit (PoB chance)", true end
	if kind == "PoB skill type" then
		if t:find("random element", 1, true) then return "some hits (random element)", false end
		return "skill in build", true
	end
	if t:find("minimum", 1, true) then return "minimum count only", false end
	-- a curse/mark gem in the build keeps the enemy cursed whatever else its description says it does on kill
	if var == "conditionEnemyCursed" and text:match("^gem ") and (t:find("curse", 1, true) or t:find("%f[%a]mark%f[%A]")) then
		return "skill in build", true
	end
	if t:find("on kill", 1, true) or t:find("killing", 1, true) or t:find("when you kill", 1, true) then return "on kill", false end
	-- stealing needs an enemy that has charges: real while mapping, nothing against a lone boss
	if t:find("%f[%a]steal") then return "steal (needs an enemy with charges)", false end
	-- a gem's own description describes the gem (a warcry gem "performs a warcry"): the mechanic IS in the build.
	-- Checked before the needs-list below, which is written for passives and items that talk about other skills.
	if text:match("^gem ") then return "skill in build", true end
	-- a trigger that needs a mechanic the build doesn't use is not a source for this build
	local ms = build and build.calcsTab and build.calcsTab.mainEnv and build.calcsTab.mainEnv.player and build.calcsTab.mainEnv.player.mainSkill
	local types = ms and ms.skillTypes or {}
	local flags = ms and ms.skillFlags or {}
	local needs = { { "trap", types[SkillType.Trapped] or flags.trap }, { "mine", types[SkillType.RemoteMined] or flags.mine }, { "totem", types[SkillType.SummonsTotem] or flags.totem },
		{ "minion", flags.minion }, { "warcry", types[SkillType.Warcry] }, { "brand", types[SkillType.Brand] },
		{ "when you block", false }, { "on block", false }, { "stun", false }, { "with bows", flags.bow },
		{ "channelling", types[SkillType.Channel] } }
	for _, n in ipairs(needs) do
		if t:find(n[1], 1, true) and not n[2] then return "needs " .. n[1] .. " (not in this build)", false end
	end
	if t:find("while moving", 1, true) then return "while moving", false end
	-- "if you haven't moved recently" / "while stationary" is never true for a main skill that moves the character
	-- (Flicker Strike, Leap Slam, Shield Charge...): the condition is dropped for them
	if t:find("haven't moved", 1, true) or t:find("stationary", 1, true) or t:find("not moved", 1, true) then
		if types[SkillType.Movement] then return "while stationary (main skill is a movement skill)", false end
		return "while stationary", true
	end
	-- periodic and aura sources need no action at all ("Every second, inflict Withered on nearby Enemies",
	-- "your Skitterbot's Aura applies Socketed Hex", "Nearby Enemies are Hindered")
	if t:find("every %d*%s*second") or t:find("nearby enem") or t:find("%f[%a]aura") then return "periodic/aura", true end
	if t:find("on hit", 1, true) or t:find("when you hit", 1, true) or t:find("on melee hit", 1, true) or t:find("chance to", 1, true) then
		return "on hit", true
	end
	if text:match("^gem ") then return "skill in build", true end
	return "other", false
end

-- evidence for one conditional option: list of { kind, text }, plus counter-evidence and PoB values
local function conditionEvidence(var, vd, lines, gems)
	local ev, counter, values = {}, {}, {}
	local o = build.calcsTab.mainOutput
	local env = build.calcsTab.mainEnv
	local mainSkill = env.player.mainSkill
	-- PoB's own gates: the option exists only for this skill / capability
	if vd and vd.ifSkill then
		for _, sk in ipairs(type(vd.ifSkill) == "table" and vd.ifSkill or { vd.ifSkill }) do
			if gems[sk] then t_insert(ev, { kind = "PoB option", text = sk .. " is in an enabled skill group (PoB ties this option to it)" }) end
		end
	end
	if vd and type(vd.ifFlag) == "string" and vd.ifFlag:match("^Condition:CanGain") and env.player.modDB:Flag(nil, vd.ifFlag) then
		t_insert(ev, { kind = "PoB flag", text = vd.ifFlag .. " is set by the build" })
	end
	-- PoB-calculated chance per hit of the main skill (its configured part)
	local ch = CONDITION_CHANCE[var]
	if ch then
		local c = tonumber(o[ch[1]]) or 0
		if c > 0 then t_insert(ev, { kind = "PoB calculation", text = ("main skill: %.0f%% chance to %s per hit"):format(c, ch[2]) })
		else t_insert(counter, ("main skill (as configured): 0%% chance to %s per hit"):format(ch[2])) end
	end
	-- the enemy was hit by an element: an enabled skill that can deal it
	local element = var:match("^enemyConditionHitBy(%a+)Damage$")
	if var == "conditionEnemyChilled" then element = "Cold" end
	if element and SkillType[element] then
		for _, sk in ipairs(env.player.activeSkillList or {}) do
			local ge = sk.activeEffect and sk.activeEffect.grantedEffect
			if ge and sk.skillTypes and sk.skillTypes[SkillType[element]] and not (sk.skillFlags and sk.skillFlags.disable) then
				local random = sk.skillTypes[SkillType.RandomElement] and " (random element: on some hits only)" or ""
				t_insert(ev, { kind = "PoB skill type", text = ge.name .. " can deal " .. element .. " damage" .. random })
			end
		end
	end
	-- source lines in passives, gear and gems
	local kws = keywordsFor(var)
	if kws then
		local seen = {}
		for _, l in ipairs(lines) do
			for _, kw in ipairs(kws) do
				local hit
				if l.name then
					-- a gem name counts when it carries the keyword AND its qualifier ("charge" for use*Charges), so
					-- "Frenzy" is judged by its description (gives a frenzy charge on hit), "Power Siphon" by its on-kill text
					local pat, also = kw, nil
					if type(kw) == "table" then pat, also = kw[1], kw[2] end
					local lname = l.text:lower()
					hit = lname:find(pat) ~= nil and (not also or lname:find(also, 1, true) ~= nil)
					for _, bad in ipairs(GEM_NAME_NOT_SOURCE[var] or {}) do if lname == bad then hit = false end end
				else hit = lineIsSource(l.text, kw) end
				if hit and not seen[l.src .. l.text] then
					seen[l.src .. l.text] = true
					t_insert(ev, { kind = "source line", text = l.src .. ": " .. l.text })
				end
			end
		end
	end
	-- supports known by name (their descriptions don't say what they generate)
	for _, l in ipairs(lines) do
		if l.name then
			local srcs = GEM_NAME_SOURCES[l.text:lower()]
			if srcs then
				for _, v in ipairs(srcs) do
					if v == var then
						t_insert(ev, { kind = "source line", text = l.src .. ": " .. l.text .. ((l.text:lower() == "innervate") and " (on kill)" or " (on detonation)") })
					end
				end
			end
		end
	end
	-- PoB's values for the configured state (context, not evidence of generation)
	local charge = var:match("^use(%u%a-)Charges$")
	if charge and o[charge .. "ChargesMax"] then
		values.charges, values.max, values.duration = o[charge .. "Charges"], o[charge .. "ChargesMax"], o[charge .. "ChargesDuration"]
		values.minimum = o[charge .. "ChargesMin"]
	elseif var == "multiplierRage" then values.max = o.MaximumRage
	elseif var == "buffFortification" then values.stacks, values.max = o.FortificationStacks, o.MaximumFortification end
	local sustaining = false
	for _, e in ipairs(ev) do
		e.trigger, e.sustaining = triggerOf(e.kind, e.text, var)
		if e.sustaining then sustaining = true end
	end
	local status = #ev > 0 and "source found" or (kws or ch or element) and "no source found" or "not assessed"
	return { status = status, sustaining_source = sustaining, evidence = ev, counter = counter,
		pob_values = next(values) and values or nil }
end

-- One recalculation. PoB's own frame (buildFlag + OnFrame) runs four calculation passes: MAIN,
-- the CALCS breakdown pass and two CALCULATOR baselines for the UI's comparison tools. The driver
-- only ever reads the MAIN output, so measurements run that pass alone, with the same cache wipe
-- and socket-group refresh the frame does. measure(true) runs the full frame.
local function measure(full)
	resp.recalcs = (resp.recalcs or 0) + 1
	build.configTab:BuildModList()
	if full or os.getenv("POB_FULL_FRAME") == "1" then
		build.buildFlag = true
		frame()
	else
		wipeGlobalCache()
		build.outputRevision = (build.outputRevision or 0) + 1
		build.buildFlag = false
		build.skillsTab:UpdateSocketGroups()
		for _, node in pairs(build.spec.nodes) do node.finalModList = node.modList end
		build.calcsTab.mainEnv = build.calcsTab.calcs.buildOutput(build, "MAIN")
		build.calcsTab.mainOutput = build.calcsTab.mainEnv.player.output
	end
	local o = build.calcsTab.mainOutput
	local dps = math.max(tonumber(o.FullDPS) or 0, tonumber(o.CombinedDPS) or 0)
	local minEle = math.min(o.FireResist or 0, o.ColdResist or 0, o.LightningResist or 0)
	local minHit = math.min(o.PhysicalMaximumHitTaken or 0, o.FireMaximumHitTaken or 0, o.ColdMaximumHitTaken or 0,
		o.LightningMaximumHitTaken or 0)
	local disabled = build.calcsTab.mainEnv.player.mainSkill and build.calcsTab.mainEnv.player.mainSkill.skillFlags
		and build.calcsTab.mainEnv.player.mainSkill.skillFlags.disable or false
	return { dps = dps, ehp = o.TotalEHP or 0, life = o.Life or 0, es = o.EnergyShield or 0, min_ele_res = minEle,
		chaos_res = o.ChaosResist or 0, min_max_hit = minHit, main_skill_disabled = disabled or nil,
		net_life_regen = o.NetLifeRegen or 0, life_leech_rate = o.LifeLeechGainRate or 0,
		net_es_regen = o.NetEnergyShieldRegen or 0, freeze_avoid = o.FreezeAvoidChance or 0,
		max_hit = { Physical = o.PhysicalMaximumHitTaken, Fire = o.FireMaximumHitTaken, Cold = o.ColdMaximumHitTaken,
			Lightning = o.LightningMaximumHitTaken, Chaos = o.ChaosMaximumHitTaken } }
end

function finiteTable(t)
	local out = {}
	for k, v in pairs(t or {}) do
		if type(v) == "number" and v == v and v ~= math.huge and v ~= -math.huge then out[k] = math.floor(v + 0.5) end
	end
	return out
end

local function delta(base, s)
	local function pct(a, b) return b ~= 0 and round((a / b - 1) * 100, 1) or nil end
	return { dps_pct = pct(s.dps, base.dps), ehp_pct = pct(s.ehp, base.ehp), life = round(s.life - base.life, 0),
		min_ele_res = s.min_ele_res, chaos_res = s.chaos_res, min_max_hit = round(s.min_max_hit, 0),
		main_skill_disabled = s.main_skill_disabled,
		dps = s.dps, ehp = round(s.ehp, 0), net_life_regen = round(s.net_life_regen, 1),
		life_leech_rate = round(s.life_leech_rate, 1), net_es_regen = round(s.net_es_regen, 1),
		freeze_avoid = round(s.freeze_avoid, 0), max_hit = finiteTable(s.max_hit) }
end

local runAuditsBody
local function runAudits()
	-- With PoB's "Auto" character level, a tree change re-derives the level from points spent, one
	-- calculation late. Pin the level while measuring, so removing a passive doesn't also de-level the
	-- character and the values read at the end belong to the build as given.
	local auto = build.characterLevelAutoMode
	build.characterLevelAutoMode = false
	local ok, res = pcall(runAuditsBody)
	build.characterLevelAutoMode = auto
	if not ok then error(res, 0) end
	-- the defence values are read last: they must match the starting point
	local b, d = res.baseline or {}, res.defence or {}
	if (d.Life and b.life and math.abs(d.Life - b.life) > 0.5) or (d.TotalEHP and b.ehp and math.abs(d.TotalEHP - b.ehp) > 1) then
		warn(("Audit: the build did not return to its starting point (life %s -> %s, EHP %s -> %s); treat audit numbers with care")
			:format(tostring(b.life), tostring(d.Life), tostring(b.ehp), tostring(d.TotalEHP)))
	end
	return res
end
runAuditsBody = function()
	local audit = {}
	local base = measure()
	audit.baseline = base

	-- 1) Non-default configuration: what each enabled option is worth
	local varList = LoadModule("Modules/ConfigOptions")
	local varByName = {}
	for _, v in ipairs(varList or {}) do if v.var then varByName[v.var] = v end end
	local ct = build.configTab
	local cfg, conditional = {}, {}
	for k, v in pairs(ct.input) do
		local def = ct:GetDefaultState(k, type(v))
		if v ~= def then
			local vd = varByName[k]
			local e = { var = k, value = v, label = vd and vd.label and stripColor(vd.label) or k, category = configCategory(k) }
			if e.category ~= "permanent" then
				ct.input[k] = def
				e.without = delta(base, measure())
				ct.input[k] = v
				t_insert(conditional, { k, v, def })
			end
			t_insert(cfg, e)
		end
	end
	if #conditional > 1 then
		for _, c in ipairs(conditional) do ct.input[c[1]] = c[3] end
		audit.without_all_conditional_config = delta(base, measure())
		for _, c in ipairs(conditional) do ct.input[c[1]] = c[2] end
	end
	measure()
	-- evidence for each conditional option, stack checks for counts, and the evidence-supported scenario
	local lines, gems = buildSourceLines(), enabledGemNames()
	local unsourced = {}
	for _, e in ipairs(cfg) do
		if e.without then
			local vd = varByName[e.var]
			local ok, info = pcall(conditionEvidence, e.var, vd, lines, gems)
			if ok then
				e.evidence = info
				if info.status == "no source found" then t_insert(unsourced, e) end
			else
				e.evidence = { status = "not assessed", evidence = {}, counter = {}, error = tostring(info) }
			end
			if vd and vd.type == "count" and type(e.value) == "number" and e.value > 0 then
				-- doubling (not +1: some counts act in steps) that changes nothing means PoB clamps the value
				local v = e.value
				ct.input[e.var] = v * 2
				local up = measure()
				ct.input[e.var] = math.floor(v / 2)
				local half = delta(base, measure())
				ct.input[e.var] = v
				e.stack = { value = v, half = math.floor(v / 2), at_half = half,
					at_pob_cap = (math.abs(up.dps - base.dps) <= math.max(1, 1e-6 * base.dps) and math.abs(up.ehp - base.ehp) <= 1) }
			end
		end
	end
	-- two scenarios between "configured" and "floor": conditions with no source found turned off; and
	-- additionally those whose sources can't be assumed to sustain them (on kill, while moving, ...)
	local function scenario(list)
		if #list == 0 then return { turned_off = {} } end
		for _, e in ipairs(list) do ct.input[e.var] = ct:GetDefaultState(e.var, type(e.value)) end
		local names = {}
		for _, e in ipairs(list) do t_insert(names, e.var) end
		local r = { without = delta(base, measure()), turned_off = names }
		for _, e in ipairs(list) do ct.input[e.var] = e.value end
		return r
	end
	if #conditional > 0 then
		local unsustained = {}
		for _, e in ipairs(cfg) do
			if e.without and e.evidence and e.evidence.status ~= "not assessed" and not e.evidence.sustaining_source then
				t_insert(unsustained, e)
			end
		end
		audit.evidence_supported = scenario(unsourced)
		audit.sustained_sources_only = scenario(unsustained)
	end
	measure()
	table.sort(cfg, function(a, b) return a.var < b.var end)
	audit.config = cfg
	audit.enemy_is_boss = ct.input.enemyIsBoss or ct:GetDefaultState("enemyIsBoss", "string")

	-- 2) Flasks: PoB counts active flasks as permanently up; show what they are worth
	local flasks, activeSlots = {}, {}
	for i = 1, 5 do
		local slot = build.itemsTab.slots["Flask " .. i]
		local item = slot and build.itemsTab.items[slot.selItemId]
		if item and slot.active then
			slot.active = false
			t_insert(flasks, { slot = "Flask " .. i, name = item.title or item.name, without = delta(base, measure()) })
			slot.active = true
			t_insert(activeSlots, slot)
		end
	end
	if #activeSlots > 1 then
		for _, s in ipairs(activeSlots) do s.active = false end
		audit.without_all_flasks = delta(base, measure())
		for _, s in ipairs(activeSlots) do s.active = true end
	end
	-- floor: no flasks and no conditional config at the same time
	if #activeSlots + #conditional > 0 then
		for _, s in ipairs(activeSlots) do s.active = false end
		for _, c in ipairs(conditional) do ct.input[c[1]] = c[3] end
		audit.floor_no_flasks_no_conditional_config = delta(base, measure())
		for _, s in ipairs(activeSlots) do s.active = true end
		for _, c in ipairs(conditional) do ct.input[c[1]] = c[2] end
	end
	measure()
	audit.flasks = flasks

	-- 2b) Every gear and flask slot: what removing its item changes (one calculation each, same baseline)
	local okSlots, slotRes = pcall(function()
		local cf = build.calcsTab:GetMiscCalculator()
		return Cand.slotContributions(cf, Cand.metrics(cf({})))
	end)
	if okSlots then audit.slots = slotRes else warn("Slot audit failed: " .. tostring(slotRes)) end

	-- 3) Non-main skill groups (auras, curses, banners, guards, buffs): contribution of each
	local groups = {}
	for gi, sg in ipairs(build.skillsTab.socketGroupList) do
		if gi ~= build.mainSocketGroup and sg.enabled and not sg.source and #sg.gemList > 0 then
			local names = {}
			for _, g in ipairs(sg.gemList) do if trim(g.nameSpec or "") ~= "" then t_insert(names, g.nameSpec) end end
			sg.enabled = false
			build.skillsTab:ProcessSocketGroup(sg)
			t_insert(groups, { index = gi, slot = sg.slot, gems = names, without = delta(base, measure()) })
			sg.enabled = true
			build.skillsTab:ProcessSocketGroup(sg)
		end
	end
	measure()
	audit.skill_groups = groups

	-- 3a) Guard skills: what is used, PoB's own uptime for it, and max hits with the guard down
	local guards = {}
	local charLevel = build.characterLevel or 90
	local attrs = { Str = build.calcsTab.mainOutput.Str or 0, Dex = build.calcsTab.mainOutput.Dex or 0,
		Int = build.calcsTab.mainOutput.Int or 0 }
	-- gem attribute requirement at a level, with PoB's own formula
	local function attrNeed(gd, l)
		local lv = gd.grantedEffect.levels[l]
		local need = {}
		for _, a in ipairs({ "Str", "Dex", "Int" }) do
			need[a] = calcLib.getGemStatRequirement(lv and lv.levelRequirement or 1, gd.grantedEffect.support, gd["req" .. a] or 0)
		end
		return need
	end
	local function meets(need)
		for a, v in pairs(need) do if v > attrs[a] then return false end end
		return true
	end
	local function bestLevel(gd, limitReq, withAttrs)
		local best
		for l, lv in ipairs((gd and gd.grantedEffect and gd.grantedEffect.levels) or {}) do
			if l <= (gd.naturalMaxLevel or 20) and lv.levelRequirement and lv.levelRequirement <= limitReq
				and (not withAttrs or meets(attrNeed(gd, l))) then best = l end
		end
		return best
	end
	local automation = build.data.gems and (function()
		for _, gd in pairs(build.data.gems) do if gd.name == "Automation" then return gd end end
	end)()
	for gi, sg in ipairs(build.skillsTab.socketGroupList) do
		if sg.enabled and not sg.source and gi ~= build.mainSocketGroup then
			local guardGem, trigger
			for _, gem in ipairs(sg.gemList) do
				local ge = gem.gemData and gem.gemData.grantedEffect
				if ge and ge.skillTypes and ge.skillTypes[SkillType.Guard] and not ge.support and gem.enabled ~= false then guardGem = gem end
				if gem.gemData and (gem.gemData.grantedEffect.isTrigger or gem.nameSpec == "Automation") and gem.enabled ~= false then
					trigger = gem
				end
			end
			if guardGem then
				local entry = { group = gi, guard = guardGem.nameSpec, guard_level = guardGem.level,
					trigger = trigger and trigger.nameSpec or "self-cast", trigger_level = trigger and trigger.level or nil }
				local gLv = guardGem.gemData.grantedEffect.levels[guardGem.level]
				entry.guard_requirement = gLv and gLv.levelRequirement
				-- PoB's own uptime for the guard: calculate it as the main skill for a moment
				local keepMain, keepActive = build.mainSocketGroup, sg.mainActiveSkill
				build.mainSocketGroup = gi
				build.skillsTab:ProcessSocketGroup(sg)
				for si, act in ipairs(sg.displaySkillList or {}) do
					if act.activeEffect and act.activeEffect.grantedEffect.skillTypes[SkillType.Guard] then sg.mainActiveSkill = si end
				end
				measure()
				local mo = build.calcsTab.mainOutput
				entry.duration, entry.cooldown, entry.uptime_pct = mo.Duration, mo.Cooldown, mo.DurationUptime
				build.mainSocketGroup, sg.mainActiveSkill = keepMain, keepActive
				build.skillsTab:ProcessSocketGroup(sg)
				-- facts for choosing a setup (from gem data, not advice)
				local tlim = trigger and trigger.gemData and gemStatAt(trigger.gemData.grantedEffect, TRIGGER_LIMIT_STAT, trigger.level)
				entry.trigger_requirement_limit = tlim
				entry.best_guard_level_for_trigger = tlim and bestLevel(guardGem.gemData, tlim) or nil
				entry.best_guard_level_at_character_level = bestLevel(guardGem.gemData, charLevel)
				entry.best_guard_level_with_attributes = bestLevel(guardGem.gemData, charLevel, true)
				local top = entry.best_guard_level_at_character_level
				entry.attributes_for_best_level = top and attrNeed(guardGem.gemData, top) or nil
				entry.attributes = attrs
				local aLv = automation and automation.grantedEffect.levels[1]
				entry.automation_requirement = aLv and aLv.levelRequirement
				entry.automation_attributes_ok = automation and meets(attrNeed(automation, 1)) or false
				t_insert(guards, entry)
			end
		end
	end
	if #guards > 0 then
		measure()
		for _, g in ipairs(guards) do
			for _, sgr in ipairs(groups) do
				if sgr.index == g.group then g.without = sgr.without end
			end
		end
	end
	audit.guards = guards
	audit.guard_absorb = build.calcsTab.mainOutput.sharedGuardAbsorb

	-- 3b) Main skill stages / parts: DPS at the minimum stage count and at each skill part,
	-- so a maximum-stage or best-part assumption is visible. Each setting is restored afterwards.
	local ms = build.calcsTab.mainEnv.player.mainSkill
	local msc = mainSkillConfig(ms)
	local src = ms and ms.activeEffect and ms.activeEffect.srcInstance
	if msc and src then
		audit.main_skill_config = msc
		if msc.stages and msc.stages.used > msc.stages.min then
			local keep, keepCalcs = src.skillStageCount, src.skillStageCountCalcs
			src.skillStageCount, src.skillStageCountCalcs = msc.stages.min, msc.stages.min
			audit.min_stage = { stages = msc.stages.min, without = delta(base, measure()) }
			src.skillStageCount, src.skillStageCountCalcs = keep, keepCalcs
		end
		if msc.part and msc.part.count > 1 then
			local keep, keepCalcs = src.skillPart, src.skillPartCalcs
			local parts = {}
			for i, name in ipairs(msc.part.names) do
				if i ~= msc.part.index then
					src.skillPart, src.skillPartCalcs = i, i
					t_insert(parts, { index = i, name = name, without = delta(base, measure()) })
				end
			end
			src.skillPart, src.skillPartCalcs = keep, keepCalcs
			audit.other_parts = parts
		end
		local back = measure()
		if math.abs(back.dps - base.dps) > math.max(1, 1e-6 * base.dps) then
			warn("Audit: the main skill did not return to its configured stage/part; treat stage numbers with care")
		end
	end

	-- 4) Uniques: how much the build depends on each (removed = slot left empty)
	local uniques = {}
	for slotName, slot in pairs(build.itemsTab.slots) do
		local item = build.itemsTab.items[slot.selItemId]
		if item and (item.rarity == "UNIQUE" or item.rarity == "RELIC") and not slotName:match("^Flask")
			and (not slot.nodeId or build.spec.allocNodes[slot.nodeId]) then
			local id = slot.selItemId
			slot:SetSelItemId(0)
			t_insert(uniques, { slot = slotName, name = item.title or item.name, without = delta(base, measure()) })
			slot:SetSelItemId(id)
		end
	end
	measure()
	table.sort(uniques, function(a, b) return (a.without.dps_pct or 0) < (b.without.dps_pct or 0) end)
	audit.uniques = uniques

	-- 4b) Reference analysis only: what each main-skill support and each leaf passive contributes.
	-- One thing at a time is switched off, recalculated and restored, like the checks above.
	if REFERENCE then
		local sg = build.skillsTab.socketGroupList[build.mainSocketGroup]
		local supports = {}
		if sg then
			for _, gem in ipairs(sg.gemList) do
				local gd = gem.gemData
				if gd and gd.grantedEffect and gd.grantedEffect.support and gem.enabled ~= false then
					gem.enabled = false
					build.skillsTab:ProcessSocketGroup(sg)
					t_insert(supports, { gem = gem.nameSpec, level = gem.level, quality = gem.quality, without = delta(base, measure()) })
					gem.enabled = true
					build.skillsTab:ProcessSocketGroup(sg)
				end
			end
		end
		audit.main_supports = supports
		-- leaf nodes only (nothing else depends on them), so removing one frees exactly one point
		local spec = build.spec
		local nodes = {}
		local leaves = {}
		for _, node in pairs(spec.allocNodes) do
			if (node.type == "Keystone" or node.type == "Notable") and node.depends and #node.depends == 1 then
				t_insert(leaves, node)
			end
		end
		table.sort(leaves, function(a, b) return (a.dn or "") < (b.dn or "") end)
		for i, node in ipairs(leaves) do
			if i > 60 then break end
			spec:DeallocNode(node)
			local m = measure()
			spec:AllocNode(node, { node })
			t_insert(nodes, { name = node.dn, type = node.type, ascendancy = node.ascendancyName, without = delta(base, m) })
		end
		local after = measure()
		if math.abs(after.dps - base.dps) > math.max(1, 1e-6 * base.dps) or math.abs(after.ehp - base.ehp) > 1 then
			warn("Reference audit: the build did not return to its baseline after the node checks; treat node numbers with care")
		end
		table.sort(nodes, function(a, b) return (a.without.dps_pct or 0) + (a.without.ehp_pct or 0)
			< (b.without.dps_pct or 0) + (b.without.ehp_pct or 0) end)
		audit.leaf_nodes = nodes
		audit.leaf_nodes_note = "Each node removed alone (its travel nodes stay allocated): a sensitivity test, not an isolated value."
	end

	-- 5) Raw defence metrics for the threshold audit
	local o = build.calcsTab.mainOutput
	local def = {}
	for _, k in ipairs(DEFENCE_KEYS) do
		local v = o[k]
		if type(v) == "number" and v == v and v ~= math.huge and v ~= -math.huge then def[k] = round(v, 2) end
	end
	audit.defence = def
	-- immunity is reported as a flag / an infinite max hit, which the numeric list above drops
	audit.chaos_immune = (o.ChaosInoculation == true) or (o.ChaosMaximumHitTaken == math.huge) or nil
	return audit
end

-- What PoB actually used for the main skill: selected part and stage count (read back after the calc)
function mainSkillConfig(skill)
	if not (skill and skill.activeEffect and skill.activeEffect.grantedEffect) then return nil end
	local ge = skill.activeEffect.grantedEffect
	local src = skill.activeEffect.srcInstance or {}
	local cfg = { name = ge.name }
	local parts = partNames(ge)
	if #parts > 0 then
		local idx = skill.skillPart or src.skillPart or 1
		cfg.part = { index = idx, name = parts[idx], count = #parts, names = parts, explicit = explicitParts[ge.name] ~= nil }
		local area = {}
		for i, p in ipairs(ge.parts or {}) do area[i] = p.area and true or false end
		cfg.part.area = area
	end
	-- PoB's skill type for skills that pick an element per use (Elemental Hit, Wild Strike, ...)
	if skill.skillTypes and SkillType.RandomElement and skill.skillTypes[SkillType.RandomElement] then cfg.random_element = true end
	local key = ge.name:gsub("%s+", "")
	local limit = skill.skillData and skill.skillData.stagesMax
	if limit and limit > 0 and skill.skillModList then
		local minStage = 1 + skill.skillModList:Sum("BASE", skill.skillCfg, "Multiplier:" .. key .. "MinimumStage")
		local used = skill.skillModList:Sum("BASE", skill.skillCfg, "Multiplier:" .. key .. "Stage")
		cfg.stages = { used = used, max = limit, min = minStage, explicit = explicitStages[ge.name] ~= nil,
			requested = explicitStages[ge.name] }
	end
	return cfg
end

---------------------------------------------------------------------------
-- Saved (exported) statistics versus PoB's recalculation
---------------------------------------------------------------------------
-- A PoB export stores the author's sidebar numbers as <PlayerStat>. They come from the author's PoB
-- version and settings, so neither side is assumed correct: differences are reported, and the
-- identifiable causes are tested one at a time by changing that factor and recalculating.
local SAVED_STAT_KEYS = { "FullDPS", "CombinedDPS", "TotalDPS", "AverageDamage", "Speed", "CritChance", "Life",
	"EnergyShield", "Mana", "Ward", "TotalEHP", "PhysicalMaximumHitTaken", "FireMaximumHitTaken", "ColdMaximumHitTaken",
	"LightningMaximumHitTaken", "ChaosMaximumHitTaken", "FireResist", "ColdResist", "LightningResist", "ChaosResist",
	"Armour", "Evasion" }
local SAVED_STAT_TOLERANCE_PCT = 1      -- rounding in the export; below this a difference isn't reported

local function activeConfigInputs(xmlText)
	local cfg = xmlText:match("<Config%s[^>]*>(.-)</Config>") or xmlText:match("<Config>(.-)</Config>") or ""
	local active = (xmlText:match("<Config%s[^>]-activeConfigSet=\"(%d+)\"")) or "1"
	local body = cfg
	for attrs, inner in cfg:gmatch("<ConfigSet(%s[^>]*)>(.-)</ConfigSet>") do
		if attrs:match("%sid=\"" .. active .. "\"") then body = inner end
	end
	local inputs = {}
	for tag in body:gmatch("<Input%s[^>]*/>") do
		local name = tag:match("%sname=\"([^\"]+)\"")
		if name then inputs[name] = true end
	end
	return inputs
end

local function compareSavedStats(xmlText)
	local saved = {}
	for tag in xmlText:gmatch("<PlayerStat%s[^>]*/>") do
		local k, v = tag:match("%sstat=\"([^\"]+)\""), tonumber(tag:match("%svalue=\"([^\"]+)\""))
		if k and v then saved[k] = v end
	end
	if not next(saved) then return nil end
	local function diffs()
		local o, out = build.calcsTab.mainOutput, {}
		for _, k in ipairs(SAVED_STAT_KEYS) do
			local a, b = saved[k], tonumber(o[k])
			if a and b and b == b and b ~= math.huge then
				local pct = a ~= 0 and (b / a - 1) * 100 or (b ~= 0 and 100 or 0)
				out[k] = { saved = a, recalculated = round(b, 2), diff_pct = round(pct, 2) }
			end
		end
		return out
	end
	local function material(d)
		local list = {}
		for k, e in pairs(d) do if math.abs(e.diff_pct) > SAVED_STAT_TOLERANCE_PCT then t_insert(list, k) end end
		table.sort(list)
		return list
	end
	local result = { stats = diffs(), tolerance_pct = SAVED_STAT_TOLERANCE_PCT, tested = {} }
	result.differing = material(result.stats)
	if #result.differing == 0 then return result end

	-- identifiable factors that can be changed and recalculated
	local factors = {}
	local li = resp.level_info
	if li and li.auto and li.saved and li.saved ~= li.used then
		t_insert(factors, { name = "character level", detail = ("character level %d (the build file) instead of PoB's Auto level %d"):format(li.saved, li.used),
			apply = function() build.characterLevelAutoMode = false build.characterLevel = li.saved end,
			restore = function() build.characterLevel = li.used build.characterLevelAutoMode = true end })
	end
	local inputs = activeConfigInputs(xmlText)
	if not inputs.enemyIsBoss then
		local current = build.configTab.input.enemyIsBoss
		for _, v in ipairs({ "None", "Boss", "Pinnacle", "Uber" }) do
			if v ~= current then
				t_insert(factors, { name = "boss setting", detail = ("boss setting enemyIsBoss = %s (the build doesn't set it; this PoB uses %s)"):format(v, tostring(current)),
					apply = function() build.configTab.input.enemyIsBoss = v end,
					restore = function() build.configTab.input.enemyIsBoss = current end })
			end
		end
	end
	local base = {}
	for _, k in ipairs(result.differing) do base[k] = true end
	local function try(set)
		for _, f in ipairs(set) do f.apply() end
		measure()
		local d = diffs()
		for i = #set, 1, -1 do set[i].restore() end
		local names = {}
		for _, f in ipairs(set) do t_insert(names, f.detail) end
		local remaining = material(d)
		local left = {}
		for _, k in ipairs(remaining) do left[k] = true end
		local resolved, introduced = {}, {}
		for _, k in ipairs(result.differing) do if not left[k] then t_insert(resolved, k) end end
		for _, k in ipairs(remaining) do if not base[k] then t_insert(introduced, k) end end
		t_insert(result.tested, { change = t_concat(names, " + "), resolves = resolved, newly_differing = introduced,
			still_differing = remaining })
		return #remaining == 0
	end
	for _, f in ipairs(factors) do
		if try({ f }) then result.explained_by = f.detail break end
	end
	-- a level change and a boss setting together
	if not result.explained_by and factors[1] and factors[1].name == "character level" then
		for i = 2, #factors do
			if try({ factors[1], factors[i] }) then result.explained_by = factors[1].detail .. " + " .. factors[i].detail break end
		end
	end
	if not result.explained_by then
		-- the change that resolves the most differences, if any: a partial explanation, labelled as such
		local best
		for _, t in ipairs(result.tested) do
			if #t.resolves > 0 and (not best or #t.resolves > #best.resolves) then best = t end
		end
		if best then
			result.partly_explained_by = { change = best.change, resolves = best.resolves, newly_differing = best.newly_differing,
				still_differing = best.still_differing }
		end
	end
	local after = measure()
	if math.abs(after.dps - resp.summary.dps) > math.max(1, 1e-6 * resp.summary.dps) or after.life ~= resp.summary.life then
		warn("Saved-stat comparison: the build did not return to its starting point; treat the numbers with care")
	end
	if not result.explained_by then
		result.unexplained = "No tested factor (character level, boss setting) reproduces all the saved numbers. Other possible causes, "
			.. "not testable here: a different PoB version or game data (the export doesn't record its PoB version; tree "
			.. tostring(build.spec.treeVersion) .. "), or settings changed after the stats were saved."
	end
	return result
end

---------------------------------------------------------------------------
-- Build profile and equipment candidate discovery.
-- Candidates come from PoB's own data: the parsed modifiers of every unique and of flask-slot bases,
-- tested against the main skill's weapon/skill flags and conditions exactly as PoB applies them.
-- A static score only orders them; a capped number of quick PoB calculations (all against one
-- baseline) measures them. Nothing here is specific to a skill, an ascendancy or an item.
---------------------------------------------------------------------------
Cand.DAMAGE = { "Physical", "Lightning", "Cold", "Fire", "Chaos" }
Cand.GEAR_SLOTS = { "Weapon 1", "Weapon 2", "Helmet", "Body Armour", "Gloves", "Boots", "Amulet", "Ring 1", "Ring 2", "Belt",
	"Flask 1", "Flask 2", "Flask 3", "Flask 4", "Flask 5" }
-- PoB outputs an item can change without moving DPS or EHP (movement, ailment avoidance, recovery)
Cand.UTILITY_KEYS = { "EffectiveMovementSpeedMod", "FreezeAvoidChance", "ChillAvoidChance", "ShockAvoidChance",
	"IgniteAvoidChance", "BleedAvoidChance", "PoisonAvoidChance", "StunAvoidChance", "LifeFlaskRecovery", "ManaFlaskRecovery",
	"LifeRegenRecovery", "ManaRegenRecovery", "LifeLeechGainRate", "ManaLeechGainRate" }
Cand.AILMENT_CHANCE = { { "Ignite", "IgniteChance" }, { "Shock", "ShockChance" }, { "Freeze", "FreezeChance" },
	{ "Bleed", "BleedChance" }, { "Poison", "PoisonChance" } }
Cand.SUPPORTED = 0.6      -- weight of a line that needs a condition the build has a source for
Cand.UNSUPPORTED = 0.15   -- ... a condition with no source found
Cand.UNKNOWN_NAME = 0.4   -- a stat nothing in the build uses yet
Cand.WRONG_TYPE = 0.25    -- a damage type the main skill doesn't deal (conversion can make this wrong)
Cand.DISCOVER = 0.5       -- static weight an item needs to count as compatible (one general stat the build uses)

function Cand.metrics(o)
	local m = {
		dps = math.max(tonumber(o.FullDPS) or 0, tonumber(o.CombinedDPS) or 0),
		ehp = tonumber(o.TotalEHP) or 0, life = tonumber(o.Life) or 0, es = tonumber(o.EnergyShield) or 0,
		min_ele_res = math.min(o.FireResist or 0, o.ColdResist or 0, o.LightningResist or 0),
		chaos_res = o.ChaosResist or 0,
		min_max_hit = math.min(o.PhysicalMaximumHitTaken or 0, o.FireMaximumHitTaken or 0, o.ColdMaximumHitTaken or 0,
			o.LightningMaximumHitTaken or 0),
		utility = {},
	}
	for _, a in ipairs({ "Str", "Dex", "Int" }) do
		local need, have = tonumber(o["Req" .. a]) or 0, tonumber(o[a]) or 0
		if need > have then
			m.attr_short = m.attr_short or {}
			m.attr_short[a] = round(need - have, 0)
		end
	end
	for _, k in ipairs(Cand.UTILITY_KEYS) do
		if type(o[k]) == "number" and o[k] == o[k] and o[k] ~= math.huge then m.utility[k] = o[k] end
	end
	return m
end

function Cand.diff(b, m)
	local function pct(a, x)
		if x == 0 then return nil end
		return round((a / x - 1) * 100, 1)
	end
	local d = { dps = round(m.dps, 1), ehp = round(m.ehp, 0), dps_pct = pct(m.dps, b.dps), ehp_pct = pct(m.ehp, b.ehp),
		life = round(m.life - b.life, 0), es = round(m.es - b.es, 0),
		min_ele_res = round(m.min_ele_res, 0), min_ele_res_change = round(m.min_ele_res - b.min_ele_res, 0),
		chaos_res = round(m.chaos_res, 0), chaos_res_change = round(m.chaos_res - b.chaos_res, 0),
		min_max_hit = round(m.min_max_hit, 0), min_max_hit_pct = pct(m.min_max_hit, b.min_max_hit),
		attr_short = m.attr_short, main_skill_no_damage = (b.dps > 0 and m.dps <= 0) or nil }
	local u = {}
	for k, v in pairs(m.utility) do
		local bv = b.utility[k]
		if bv and math.abs(v - bv) > 1e-6 * math.max(1, math.abs(bv)) then u[k] = { from = round(bv, 2), to = round(v, 2) } end
	end
	for k, bv in pairs(b.utility) do
		if m.utility[k] == nil and bv ~= 0 then u[k] = { from = round(bv, 2), to = 0 } end
	end
	if next(u) then d.utility_changed = u end
	return d
end

local function candItemAt(slotName)
	local slot = build.itemsTab.slots[slotName]
	local item = slot and slot.selItemId and slot.selItemId > 0 and build.itemsTab.items[slot.selItemId]
	return slot, item
end

-- What each gear/flask slot contributes: its item removed, everything else unchanged (one calculation each)
function Cand.slotContributions(cf, base)
	local out = {}
	for _, slotName in ipairs(Cand.GEAR_SLOTS) do
		local slot, item = candItemAt(slotName)
		if slot and not slot.inactive then
			if not item then
				t_insert(out, { slot = slotName, empty = true })
			elseif not (slotName:match("^Flask") and not slot.active) then
				resp.recalcs = (resp.recalcs or 0) + 1
				local d = Cand.diff(base, Cand.metrics(cf({ repSlotName = slotName })))
				local none = math.abs(d.dps_pct or 0) < 0.5 and math.abs(d.ehp_pct or 0) < 0.5 and not d.main_skill_no_damage
				t_insert(out, { slot = slotName, name = item.title or item.name, rarity = item.rarity, base = item.baseName,
					type = item.type, without = d, no_measured_benefit = none or nil,
					other_effects = (d.utility_changed or d.attr_short or (d.min_ele_res_change or 0) ~= 0
						or (d.chaos_res_change or 0) ~= 0) and true or nil })
			end
		end
	end
	return out
end

local function sortedKeys(t)
	local out = {}
	for k, v in pairs(t) do if v then t_insert(out, k) end end
	table.sort(out)
	return out
end

-- The build's mechanics as PoB calculated them. withParts reads every part of a multi-part skill
-- (one recalculation each), so a random-element skill counts all its elements.
function Cand.profile(withParts)
	local env = build.calcsTab.mainEnv
	local o = build.calcsTab.mainOutput
	local ms = env.player.mainSkill
	local spec = build.spec
	local asc = spec.curAscendClass and spec.curAscendClass.name ~= "None" and spec.curAscendClass.name or nil
	local p = { class = spec.curClassName, ascendancy = asc, damage_types = {}, ailment_chance = {} }
	if not (ms and ms.activeEffect and ms.activeEffect.grantedEffect) then return p end
	local ge = ms.activeEffect.grantedEffect
	p.main_skill = ge.name
	local types = {}
	for name, id in pairs(SkillType) do if ms.skillTypes and ms.skillTypes[id] then types[name] = true end end
	p.skill_types = sortedKeys(types)
	p.skill_flags = sortedKeys(ms.skillFlags or {})
	p.skill_weapon_types = sortedKeys(ge.weaponTypes or {})
	p.weapon_class = env.player.weaponData1 and env.player.weaponData1.type or "None"
	local w2 = env.player.itemList and env.player.itemList["Weapon 2"]
	p.offhand = w2 and w2.base and w2.base.type or nil
	local function read(out)
		local total = 0
		for _, t in ipairs(Cand.DAMAGE) do total = total + (tonumber(out[t .. "HitAverage"]) or 0) end
		for _, t in ipairs(Cand.DAMAGE) do
			local v = tonumber(out[t .. "HitAverage"]) or 0
			if v > 0 and total > 0 then p.damage_types[t] = math.max(p.damage_types[t] or 0, round(v / total * 100, 0)) end
		end
		for _, a in ipairs(Cand.AILMENT_CHANCE) do
			local c = tonumber(out[a[2]]) or 0
			if c > 0 then p.ailment_chance[a[1]] = math.max(p.ailment_chance[a[1]] or 0, round(c, 0)) end
		end
	end
	local function readAll(out)
		read(out)
		if out.MainHand then read(out.MainHand) end
		if out.OffHand then read(out.OffHand) end
	end
	readAll(o)
	local msc = mainSkillConfig(ms)
	if msc and msc.part and msc.part.count > 1 then
		p.part = { index = msc.part.index, name = msc.part.name, count = msc.part.count }
		local src = ms.activeEffect.srcInstance
		if withParts and src then
			local keep, keepCalcs = src.skillPart, src.skillPartCalcs
			p.parts_read = { msc.part.name }
			for i, name in ipairs(msc.part.names) do
				if i ~= msc.part.index and i <= 8 then
					src.skillPart, src.skillPartCalcs = i, i
					measure()
					readAll(build.calcsTab.mainOutput)
					t_insert(p.parts_read, name)
				end
			end
			src.skillPart, src.skillPartCalcs = keep, keepCalcs
			measure()
			o = build.calcsTab.mainOutput
		end
	end
	-- damage types without hits (damage over time): the skill's own type tags
	if not next(p.damage_types) then
		for _, t in ipairs(Cand.DAMAGE) do if types[t] then p.damage_types[t] = 0 end end
	end
	local hit = tonumber(o.TotalDPS) or 0
	local comb = math.max(tonumber(o.CombinedDPS) or 0, tonumber(o.FullDPS) or 0, hit)
	p.delivery = (types.Minion or types.CreatesMinion) and "minion" or types.Attack and "attack" or types.Spell and "spell" or "other"
	p.hit_or_dot = (comb > 0 and hit / comb < 0.5) and "damage over time" or "hit"
	local dt = p.damage_types
	local ele = (dt.Fire or 0) + (dt.Cold or 0) + (dt.Lightning or 0)
	if ele == 0 and (dt.Fire or dt.Cold or dt.Lightning) then ele = 0.5 end
	local phys, chaos = dt.Physical or (dt.Physical == 0 and 0.5) or 0, dt.Chaos or 0
	p.damage_family = (ele >= phys and ele >= chaos and ele > 0) and "elemental" or (phys >= chaos and phys > 0) and "physical"
		or chaos > 0 and "chaos" or "unknown"
	local hands = p.offhand and (p.offhand == "Shield" and "shield" or p.offhand == "Quiver" and "quiver" or "dual wield") or
		((data.weaponTypeInfo[p.weapon_class] or {}).oneHand == false and "two-handed" or "one weapon")
	p.signature = { main_skill = p.main_skill, weapon_class = p.weapon_class .. " (" .. hands .. ")", ascendancy = p.ascendancy,
		archetype = p.delivery .. ", " .. p.hit_or_dot .. ", " .. p.damage_family }
	return p
end

-- config options -> the condition each one sets, read from the option's own apply function
function Cand.conditionConfigMap()
	if Cand._condMap then return Cand._condMap, Cand._varByName end
	local map, byName = { player = {}, enemy = {} }, {}
	for _, vd in ipairs(LoadModule("Modules/ConfigOptions") or {}) do
		if vd.var then byName[vd.var] = vd end
		if vd.var and vd.type == "check" and vd.apply then
			local ml, eml = new("ModList"), new("ModList")
			if pcall(vd.apply, true, ml, eml, build) then
				for who, list in pairs({ player = ml, enemy = eml }) do
					for _, mod in ipairs(list) do
						local cond = mod.name and mod.name:match("^Condition:(.+)$")
						if cond and mod.type == "FLAG" then
							map[who][cond] = map[who][cond] or {}
							t_insert(map[who][cond], vd.var)
						end
					end
				end
			end
		end
	end
	Cand._condMap, Cand._varByName = map, byName
	return map, byName
end

-- the same skill config with each other weapon class the skill can use
function Cand.altCfgs(cfg, p)
	local out = {}
	local clear = bit.bnot(bit.bor(ModFlag.WeaponMask, ModFlag.Weapon1H, ModFlag.Weapon))
	for _, wt in ipairs(p.skill_weapon_types or {}) do
		local info = data.weaponTypeInfo[wt]
		if info and wt ~= p.weapon_class then
			local f = bit.band(cfg.flags, clear)
			f = bit.bor(f, ModFlag.Weapon, info.oneHand and ModFlag.Weapon1H or ModFlag.Weapon2H,
				info.melee and ModFlag.WeaponMelee or ModFlag.WeaponRanged, ModFlag[info.flag] or 0)
			t_insert(out, { name = wt == "None" and "no weapon (unarmed)" or wt, type = wt, cfg = { flags = f, keywordFlags = cfg.keywordFlags } })
		end
	end
	return out
end

local function cfgApplies(cfg, mod)
	return bit.band(cfg.flags or 0, mod.flags or 0) == (mod.flags or 0) and MatchKeywordFlags(cfg.keywordFlags or 0, mod.keywordFlags or 0)
end

-- one tag of a parsed modifier against the current state
function Cand.tagState(tag, ctx)
	local t = tag.type
	if t == "Condition" or t == "ActorCondition" then
		local actor = t == "ActorCondition" and (tag.actor or "player") or "player"
		local vars = tag.varList or { tag.var }
		local db = actor == "enemy" and ctx.env.enemyDB or actor == "player" and ctx.env.player.modDB or nil
		local met = false
		if db then
			for _, v in ipairs(vars) do
				local ok, r = pcall(db.GetCondition, db, v)
				if ok and r then met = true end
			end
		end
		if tag.neg then met = not met end
		return met and "ok" or "unmet", { actor = actor, vars = vars, neg = tag.neg or nil }
	elseif t == "SkillType" then
		local has = ctx.ms.skillTypes and ctx.ms.skillTypes[tag.skillType] or false
		if tag.neg then has = not has end
		return has and "ok" or "other skill"
	elseif t == "SkillName" then
		local hit = false
		for _, n in ipairs(tag.skillNameList or { tag.skillName }) do if n == ctx.cfg.skillName then hit = true end end
		return hit and "ok" or "other skill"
	elseif t == "SkillId" then
		return (ctx.ms.activeEffect.grantedEffect.id == tag.skillId) and "ok" or "other skill"
	end
	return "ok"    -- multipliers, per-stat, slot and socket tags: scaling, measured by PoB
end

function Cand.conditionSupport(conds, ctx)
	local res = { supported = false, details = {} }
	for _, c in ipairs(conds) do
		for _, v in ipairs(c.vars) do
			local cfgVars = ctx.condMap[c.actor == "enemy" and "enemy" or "player"][v] or {}
			local entry = { actor = c.actor, condition = v, negated = c.neg, config = cfgVars[1] }
			for _, cv in ipairs(cfgVars) do
				local ev = ctx.evidence(cv)
				if ev then
					entry.config, entry.evidence, entry.sustaining_source = cv, ev.status, ev.sustaining_source
					entry.examples = {}
					for i, e in ipairs(ev.evidence or {}) do if i <= 2 then t_insert(entry.examples, e.text) end end
					if ev.status == "source found" then res.supported = true break end
				end
			end
			if not entry.config then entry.evidence = "no PoB config option sets it" end
			t_insert(res.details, entry)
		end
	end
	return res
end

function Cand.damageTypeMismatch(name, p)
	if name:find("Taken") or name:find("Resist") then return false end
	local offensive = name:find("Damage") or name:find("Penetration") or name:find("Min$") or name:find("Max$")
	if not offensive then return false end
	for _, t in ipairs(Cand.DAMAGE) do
		if name:find("^" .. t) then return p.damage_types[t] == nil end
	end
	if name:find("^Elemental") then
		return not (p.damage_types.Fire or p.damage_types.Cold or p.damage_types.Lightning)
	end
	return false
end

local function candLines(item)
	local out = {}
	for _, list in ipairs({ item.implicitModLines, item.explicitModLines, item.enchantModLines }) do
		for _, ml in ipairs(list or {}) do
			if lineIsActive(item, ml) then t_insert(out, ml) end
		end
	end
	return out
end

-- weapon-restriction bits of PoB's mod flags, with readable names
Cand.WEAPON_BITS = { { "WeaponMelee", "a melee weapon" }, { "WeaponRanged", "a ranged weapon" }, { "Weapon1H", "a one-handed weapon" },
	{ "Weapon2H", "a two-handed weapon" }, { "Bow", "a bow" }, { "Wand", "a wand" }, { "Claw", "a claw" }, { "Dagger", "a dagger" },
	{ "Sword", "a sword" }, { "Axe", "an axe" }, { "Mace", "a mace or sceptre" }, { "Staff", "a staff" }, { "Unarmed", "no weapon" },
	{ "Weapon", "a weapon" } }
function Cand.missingWeapon(cfgFlags, modFlags)
	local miss = {}
	for _, wb in ipairs(Cand.WEAPON_BITS) do
		local bitv = ModFlag[wb[1]]
		if bitv and bit.band(modFlags, bitv) ~= 0 and bit.band(cfgFlags, bitv) == 0 then t_insert(miss, wb[2]) end
	end
	return miss
end

-- static compatibility of an item: which of its lines apply to this build, under which conditions.
-- Weights only order candidates for the capped PoB measurement; a weight never accepts an item.
--   1.5  a line restricted to what this skill is (its weapon, skill or damage keywords) and used by the build
--   1.0  the same, for a stat nothing in the build has yet (scaled by size, like the others)
--   1.2  the same, needing a condition the build has a source for
--   0.5  a general stat the build already uses (life, resistances...); these add up to at most 1.5
--   0.2  a stat nothing in the build uses yet, or one that needs a condition with no source
-- A damage type the skill doesn't deal counts a quarter.
function Cand.static(item, ctx)
	local r = { score = 0, alt = {}, lines = {}, conditions = {}, unsupported = 0, restricted = {} }
	local lineSets = {}
	for _, ml in ipairs(candLines(item)) do
		t_insert(lineSets, { text = rolledLine(ml), mods = ml.modList or {}, extra = ml.extra })
	end
	if item.buffModList and item.buffModList[1] then
		t_insert(lineSets, { text = "(" .. (item.type or "item") .. " effect while active)", mods = item.buffModList })
	end
	local general, supportedConds = 0, true
	local relaxed = bit.bor(ctx.cfgs[1].flags or 0, ModFlag.WeaponMask, ModFlag.Weapon1H, ModFlag.Weapon)
	for _, ls in ipairs(lineSets) do
		if ls.extra then
			r.unsupported = r.unsupported + 1
			t_insert(r.lines, { text = ls.text, status = "not calculated by PoB" })
		else
			local best, bestGeneral, status, alts, condInfo, missing = 0, 0, "no effect on this skill", {}, nil, nil
			-- one line can hold several mods that each depend on a different condition ("for each type
			-- of ailment"); those add up, one per condition. Unconditional mods of a line count once.
			local perCond = {}
			for _, mod in ipairs(ls.mods) do
				local applies = false
				for _, c in ipairs(ctx.cfgs) do if cfgApplies(c, mod) then applies = true end end
				local known = ctx.known(mod.name)
				local specific = ((mod.flags or 0) ~= 0) or ((mod.keywordFlags or 0) ~= 0)
				local state, conds = "ok", {}
				for i = 1, #mod do
					local tag = mod[i]
					if type(tag) == "table" and tag.type then
						local s, info = Cand.tagState(tag, ctx)
						if s == "unmet" then t_insert(conds, info) if state == "ok" then state = "conditional" end
						elseif s == "other skill" then state = "other skill" end
					end
				end
				if applies and state ~= "other skill" then
					local w, lineStatus
					if not known and specific then
						-- restricted to what this skill is, but nothing in the build has it yet (a new mechanic)
						w, lineStatus = 1.0 * Cand.magnitude(mod, ctx), "applies to this skill (a stat the build doesn't have yet)"
					elseif not known then w, lineStatus = 0.2, "a stat the build doesn't use yet"
					elseif specific then w, lineStatus = 1.5 * Cand.magnitude(mod, ctx), "applies to this skill"
					else w, lineStatus = 0.5, "general stat" end
					if state == "conditional" then
						local sup = Cand.conditionSupport(conds, ctx)
						condInfo = condInfo or {}
						t_insert(condInfo, sup)
						if sup.supported and known then w = specific and 1.2 or 0.4 else w = 0.2 end
						local n = {}
						for _, d in ipairs(sup.details) do t_insert(n, (d.negated and "not " or "") .. d.actor .. " " .. d.condition) end
						lineStatus = "needs " .. t_concat(n, " or ") .. (sup.supported and " (the build has a source)" or " (no source found in the build)")
					end
					if Cand.damageTypeMismatch(mod.name or "", ctx.p) then
						w, lineStatus = w * Cand.WRONG_TYPE, "a damage type the skill doesn't deal"
					end
					local condKey
					for i = 1, #mod do
						local tag = mod[i]
						if type(tag) == "table" and (tag.type == "Condition" or tag.type == "ActorCondition") then
							condKey = (condKey or "") .. (tag.actor or "player") .. ":" .. tostring(tag.var or t_concat(tag.varList or {}, "|")) .. ";"
						end
					end
					if condKey and w > (perCond[condKey] or 0) then perCond[condKey] = w end
					local isGeneral = (w == 0.5 and not specific and state == "ok")
					if isGeneral then
						if w > bestGeneral and best == 0 then bestGeneral, status = w, lineStatus end
					elseif w > best then best, status = w, lineStatus end
				elseif state == "other skill" then
					if best == 0 and bestGeneral == 0 then status = "applies to another skill only" end
				else
					for _, a in ipairs(ctx.alts) do
						if cfgApplies(a.cfg, mod) then alts[a.name] = true end
					end
					if cfgApplies({ flags = relaxed, keywordFlags = ctx.cfgs[1].keywordFlags }, mod) then
						missing = Cand.missingWeapon(ctx.cfgs[1].flags or 0, mod.flags or 0)
					end
				end
			end
			local condSum, nCond = 0, 0
			for _, w in pairs(perCond) do condSum, nCond = condSum + w, nCond + 1 end
			if nCond > 1 and condSum > best then
				best = math.min(condSum, 4.5)
				local met, src, none = {}, {}, {}
				for k in pairs(perCond) do
					local cond = k:match(":([^;|]+)")
					local hit
					for _, ci in ipairs(condInfo or {}) do
						for _, d in ipairs(ci.details) do if d.condition == cond then hit = d end end
					end
					if not hit then t_insert(met, cond) elseif hit.evidence == "source found" then t_insert(src, cond) else t_insert(none, cond) end
				end
				table.sort(met) table.sort(src) table.sort(none)
				status = "scales with several conditions; holds now: " .. (#met > 0 and t_concat(met, ", ") or "none")
					.. "; build has a source for: " .. (#src > 0 and t_concat(src, ", ") or "none")
					.. (#none > 0 and ("; no source: " .. t_concat(none, ", ")) or "")
			end
			if best == 0 and bestGeneral == 0 then
				if next(alts) then
					status = "applies only with: " .. t_concat(sortedKeys(alts), ", ")
					for a in pairs(alts) do r.alt[a] = (r.alt[a] or 0) + 1 end
				elseif missing and #missing > 0 then
					status = "needs " .. t_concat(missing, " and ") .. " (the main skill doesn't use one)"
				end
				if missing and #missing > 0 then t_insert(r.restricted, { text = ls.text, needs = missing }) end
			end
			for _, ci in ipairs(condInfo or {}) do
				for _, d in ipairs(ci.details) do
					local dup = false
					for _, e in ipairs(r.conditions) do if e.actor == d.actor and e.condition == d.condition then dup = true end end
					if not dup then t_insert(r.conditions, d) end
				end
				if not ci.supported then supportedConds = false end
			end
			r.score = r.score + best
			general = general + bestGeneral
			t_insert(r.lines, { text = ls.text, status = status, weight = round(math.max(best, bestGeneral), 2) })
		end
	end
	r.score = r.score + math.min(general, 1.5)
	r.conditions_supported = (#r.conditions == 0) or supportedConds
	return r
end

-- how much a line adds relative to what the build already has of the same stat (PoB's own sums for
-- the main skill): 85% increased on top of 300% counts less than on top of 50%; a chance already at
-- 100% gains nothing. Only orders candidates; PoB's calculation decides.
function Cand.magnitude(mod, ctx)
	local v = type(mod.value) == "number" and mod.value or nil
	if not v or not mod.name then return 1 end
	local db, cfg = ctx.env.player.modDB, ctx.cfgs[1]
	local ok, cur = pcall(db.Sum, db, mod.type, cfg, mod.name)
	cur = ok and tonumber(cur) or 0
	local rel
	if mod.type == "INC" then rel = v / (100 + math.max(cur, 0))
	elseif mod.type == "MORE" then rel = v / 100
	elseif mod.type == "BASE" then
		if mod.name:find("Chance") then rel = math.max(0, math.min(v, 100 - cur)) / 100
		else rel = v / math.max(math.abs(cur), math.abs(v), 1) end
	else return 1 end
	return math.max(0.4, math.min(1.6, 0.4 + 3 * math.abs(rel)))
end

local function nodeText(node)
	return t_concat(nodeStats(node), " ")
end

-- item types named by passives: allocated ones, and unallocated ones within reach (incl. this ascendancy)
function Cand.nodeMentions(p)
	local spec = build.spec
	local types = {}
	for _, base in pairs(build.data.itemBases) do
		if base.type and not base.hidden and not data.weaponTypeInfo[base.type] then types[base.type] = true end
	end
	local asc = spec.curAscendClass
	local out = { allocated = {}, reachable = {}, nodes = {} }
	for id, node in pairs(spec.nodes) do
		if isRealNode(node) then
			local mine = (not node.ascendancyName) or (asc and (node.ascendancyName == asc.id or node.ascendancyName == asc.name))
			local alloc = spec.allocNodes[id] ~= nil
			local points = node.path and #node.path or nil
			local kindOk = node.type == "Notable" or node.type == "Keystone" or node.ascendancyName ~= nil
			if mine and kindOk and (alloc or node.ascendancyName or (points and points <= 8)) then
				local text = nodeText(node):lower()
				for ty in pairs(types) do
					if text:find(ty:lower(), 1, true) then
						local bucket = alloc and out.allocated or out.reachable
						bucket[ty] = bucket[ty] or {}
						t_insert(bucket[ty], node.dn)
						if not alloc then
							t_insert(out.nodes, { id = id, name = node.dn, type = node.type, ascendancy = node.ascendancyName and asc.name or nil,
								points = points, item_type = ty, text = nodeStats(node) })
						end
					end
				end
			end
		end
	end
	return out
end

-- unallocated passives that supply a condition a candidate needs (same source rules as the audit)
function Cand.conditionNodes(vars)
	local spec = build.spec
	local asc = spec.curAscendClass
	local out = {}
	for id, node in pairs(spec.nodes) do
		if isRealNode(node) and not spec.allocNodes[id] and (node.type == "Notable" or node.type == "Keystone" or node.ascendancyName) then
			local mine = (not node.ascendancyName) or (asc and (node.ascendancyName == asc.id or node.ascendancyName == asc.name))
			local points = node.path and #node.path or nil
			if mine and (node.ascendancyName or (points and points <= 6)) then
				for _, line in ipairs(nodeStats(node)) do
					for _, var in ipairs(vars) do
						for _, kw in ipairs(keywordsFor(var) or {}) do
							if lineIsSource(line, kw) then
								t_insert(out, { id = id, name = node.dn, type = node.type, ascendancy = node.ascendancyName and asc.name or nil,
									points = points, supplies = var, text = { line } })
							end
						end
					end
				end
			end
		end
	end
	return out
end

local function freshItem(raw)
	local item = new("Item", raw)
	if not item.base then return nil end
	item:NormaliseQuality()
	item:BuildAndParseRaw()
	return item
end

-- a base item as plain text with mid-rolled values (the same text is evaluated and written to the spec)
local function midRoll(l)
	if l:find("%((%-?%d+%.?%d*)%-(%-?%d+%.?%d*)%)") then return itemLib.applyRange(l, 0.5) end
	return l
end
local function baseRaw(name, base, rarity, lines, itemLevel)
	local impl = {}
	for l in tostring(base.implicit or ""):gmatch("[^\n]+") do t_insert(impl, midRoll(l)) end
	local rolled = {}
	for _, l in ipairs(lines or {}) do t_insert(rolled, midRoll(l)) end
	lines = rolled
	-- PoB's text form: a normal item repeats its base as the name; a rare has a name line then the base line
	local raw = "Rarity: " .. rarity .. "\n" .. (rarity == "RARE" and ("Template " .. name .. "\n") or "") .. name .. "\n"
		.. (rarity == "NORMAL" and (name .. "\n") or "") .. (itemLevel and ("Item Level: " .. tostring(itemLevel) .. "\n") or "") .. "Quality: 20\n"
		.. "Implicits: " .. #impl .. "\n"
	if #impl > 0 then raw = raw .. t_concat(impl, "\n") .. "\n" end
	if lines and #lines > 0 then raw = raw .. t_concat(lines, "\n") .. "\n" end
	return raw
end

-- slot groups: interchangeable slots share one pool; the target is the slot worth least
function Cand.groups(contrib, locked)
	local kinds, order = {}, {}
	for _, c in ipairs(contrib) do
		if not locked[c.slot] then
			local kind = c.slot:match("^(Ring)") or c.slot:match("^(Flask)") or c.slot
			if not kinds[kind] then kinds[kind] = { kind = kind, members = {} } t_insert(order, kind) end
			t_insert(kinds[kind].members, c)
		end
	end
	local function rank(c)
		if c.empty then return -1 end
		local v = math.abs(c.without.dps_pct or 0) + math.abs(c.without.ehp_pct or 0)
		if c.other_effects then v = v + 1000 end    -- keep items with effects DPS/EHP don't show
		return v
	end
	local out = {}
	for _, k in ipairs(order) do
		local g = kinds[k]
		table.sort(g.members, function(a, b) return rank(a) < rank(b) end)
		g.target = g.members[1]
		t_insert(out, g)
	end
	return out
end

function Cand.run(opts)
	opts = opts or {}
	local maxEvals = math.max(0, tonumber(opts.max_evals) or 60)
	local t0 = os.clock()
	local keepPerGroup = tonumber(opts.static_keep) or 4
	-- craftable flask-slot bases are a small family whose value is mostly their size: screen up to this many
	local baseScreen = tonumber(opts.base_screen) or 10
	local ilvl = tonumber(opts.item_level) or 84
	local req = opts.requirements or {}
	local out = { counts = { evaluated = 0, eval_limit = maxEvals, profile_recalcs = 0, conditional_evals = 0 } }
	local recalcs0 = resp.recalcs or 0
	out.profile = Cand.profile(true)
	out.counts.profile_recalcs = (resp.recalcs or 0) - recalcs0
	local env = build.calcsTab.mainEnv
	local ms = env.player.mainSkill
	if not (ms and ms.activeEffect) then
		out.error = "no main skill to evaluate candidates for"
		return out
	end
	local condMap, varByName = Cand.conditionConfigMap()
	local srcLines, gemNames = buildSourceLines(), enabledGemNames()
	local evCache = {}
	local cfgs = {}
	for _, c in ipairs({ ms.weapon1Cfg, ms.weapon2Cfg }) do if c then t_insert(cfgs, c) end end
	if #cfgs == 0 then t_insert(cfgs, ms.skillCfg) end
	local ctx = { env = env, ms = ms, cfg = ms.skillCfg, cfgs = cfgs, p = out.profile, condMap = condMap,
		alts = Cand.altCfgs(cfgs[1], out.profile),
		known = function(name) return name and (env.player.modDB.mods[name] ~= nil) end,
		evidence = function(var)
			if evCache[var] == nil then
				local ok, ev = pcall(conditionEvidence, var, varByName[var], srcLines, gemNames)
				evCache[var] = ok and ev or false
			end
			return evCache[var] or nil
		end }
	local cf = build.calcsTab:GetMiscCalculator()
	local base = Cand.metrics(cf({}))
	out.baseline = { dps = round(base.dps, 1), ehp = round(base.ehp, 0), life = round(base.life, 0),
		min_ele_res = round(base.min_ele_res, 0), chaos_res = round(base.chaos_res, 0), min_max_hit = round(base.min_max_hit, 0) }
	local contrib = Cand.slotContributions(cf, base)
	out.slots = contrib
	-- slots holding an item the user required stay as they are
	local locked, requiredUniques = {}, {}
	for _, u in ipairs(req.uniques or {}) do requiredUniques[lc(u)] = true end
	for _, c in ipairs(contrib) do
		if c.name and requiredUniques[lc(c.name)] then locked[c.slot] = "holds a required unique" end
	end
	out.locked = locked
	local groups = Cand.groups(contrib, locked)
	local mentions = Cand.nodeMentions(out.profile)
	out.node_mentions = { allocated = mentions.allocated, reachable = mentions.reachable }
	local level = build.characterLevel or 90
	local equippedTitles = {}
	for _, c in ipairs(contrib) do if c.name then equippedTitles[lc(c.name)] = true end end
	-- the pool: every unique, plus flask-slot bases (their implicit and buff are the item)
	local pool = {}
	for _, u in pairs(main.uniqueDB.list) do
		local rl = u.requirements and u.requirements.level or 0
		local gone = u.source and lc(u.source):find("no longer obtainable", 1, true)
		if rl <= level and not gone and not equippedTitles[lc(u.title or "")] then t_insert(pool, { kind = "unique", src = u }) end
	end
	for name, b in pairs(build.data.itemBases) do
		if not b.hidden and (b.flask or b.tincture) and ((b.req and b.req.level) or 0) <= level then
			local raw = baseRaw(name, b, "NORMAL")
			local item = freshItem(raw)
			if item then t_insert(pool, { kind = "base", src = item, base_name = name, raw = raw }) end
		end
	end
	-- items the user (or a test) supplies as plain text join the same search
	for _, x in ipairs(opts.extra_items or {}) do
		local raw = type(x) == "table" and x.text or x
		local item = raw and freshItem(raw)
		if item then t_insert(pool, { kind = "supplied", src = item, raw = raw })
		else warn("Candidate search: could not read a supplied item") end
	end
	table.sort(pool, function(a, b) return (a.src.title or a.src.name or "") < (b.src.title or b.src.name or "") end)
	out.counts.pool = #pool
	local discovered, differentConfig = 0, {}
	local traceSet = {}
	for _, want in ipairs(opts.trace or {}) do traceSet[lc(want)] = true end
	for _, g in ipairs(groups) do
		g.cands = {}
		local target = g.target.slot
		for _, entry in ipairs(pool) do
			local it = entry.src
			if build.itemsTab:IsItemValidForSlot(it, target) then
				-- a required weapon type keeps its weapon class
				local okReq = true
				if req.weapon_type and target == "Weapon 1" and lc(it.base and it.base.type or "") ~= lc(req.weapon_type) then okReq = false end
				local ictx, changesClass = ctx, nil
				if okReq and it.base and it.base.weapon and target:match("^Weapon") and it.base.type ~= out.profile.weapon_class then
					-- a weapon candidate changes the weapon: judge its lines with its own class, if the skill can use it
					local alt
					for _, a in ipairs(ctx.alts) do if a.type == it.base.type then alt = a end end
					if alt then
						ictx = setmetatable({ cfgs = { alt.cfg } }, { __index = ctx })
						changesClass = it.base.type
					elseif target == "Weapon 1" then
						okReq = false     -- the main skill can't use this weapon type
					end
				end
				if okReq and target == "Weapon 2" and not changesClass and (it.base and it.base.type) ~= out.profile.offhand then
					changesClass = it.base and it.base.type     -- a different off-hand (shield, quiver, another weapon)
				end
				local nm = lc(it.title or it.name or "")
				if traceSet[nm] and not okReq then
					out.trace = out.trace or {}
					t_insert(out.trace, { name = it.title or it.name, group = g.kind, target = target, discovered = false,
						reason = "excluded before scoring (a requirement, or a weapon type the main skill can't use)" })
				end
				if okReq then
					local st = Cand.static(it, ictx)
					if traceSet[nm] and st.score < Cand.DISCOVER then
						out.trace = out.trace or {}
						t_insert(out.trace, { name = it.title or it.name, group = g.kind, target = target, discovered = false,
							static_score = round(st.score, 2), lines = st.lines, reason = "no line applies to this build as configured" })
					end
					local named = (mentions.allocated[it.type] and 1 or 0) + (mentions.reachable[it.type] and 0.5 or 0)
					local altBest, altName = 0, nil
					for a, n in pairs(st.alt) do if n > altBest then altBest, altName = n, a end end
					-- discovered = some line applies; a passive naming the item type only adds to its rank
					if st.score >= Cand.DISCOVER then
						discovered = discovered + 1
						t_insert(g.cands, { entry = entry, st = st, score = st.score + named, named = named > 0 and (mentions.allocated[it.type]
							or mentions.reachable[it.type]) or nil, changes_weapon_class = changesClass })
					end
					if altBest >= 1 and altBest > st.score then
						t_insert(differentConfig, { name = it.title or it.name, kind = entry.kind, type = it.type, slot = target,
							needs_weapon = altName, reason = "its lines apply only with " .. altName .. " (current: " .. out.profile.weapon_class .. ")",
							lines = st.lines, score = altBest })
					elseif #st.restricted > 0 and #st.restricted * 1.5 > st.score then
						t_insert(differentConfig, { name = it.title or it.name, kind = entry.kind, type = it.type, slot = target,
							needs = st.restricted[1].needs, reason = "its main lines need " .. t_concat(st.restricted[1].needs, " and ")
							.. ", which the main skill doesn't use (current weapon: " .. out.profile.weapon_class .. "); a passive that extends them would change that",
							lines = st.lines, score = #st.restricted * 1.5 })
					end
				end
			end
		end
		table.sort(g.cands, function(a, b)
			if a.score ~= b.score then return a.score > b.score end
			return (a.entry.src.title or a.entry.src.name or "") < (b.entry.src.title or b.entry.src.name or "")
		end)
		g.discovered = #g.cands
		-- requested traces: where a named item landed (for checking the search, never used to rank)
		for _, want in ipairs(opts.trace or {}) do
			for rank, c in ipairs(g.cands) do
				local nm = c.entry.src.title or c.entry.src.name or ""
				if lc(nm) == lc(want) or lc(c.entry.base_name or "") == lc(want) then
					out.trace = out.trace or {}
					t_insert(out.trace, { name = want, group = g.kind, target = target, rank = rank, of = #g.cands, discovered = true,
						static_score = round(c.score, 2), lines = c.st.lines, kind = c.entry.kind })
				end
			end
		end
		-- keep the best of each kind (unique, craftable base) so a craftable option is always measured
		local keep, kinds = {}, {}
		for _, c in ipairs(g.cands) do
			kinds[c.entry.kind] = (kinds[c.entry.kind] or 0) + 1
			-- craftable bases are a small family whose value is mostly their size: measure a few of them
			if c.entry.kind == "supplied" or kinds[c.entry.kind] <= (c.entry.kind == "base" and baseScreen or 2) then t_insert(keep, c) end
		end
		local supplied = 0
		for _, c in ipairs(keep) do if c.entry.kind == "supplied" then supplied = supplied + 1 end end
		for _, c in ipairs(g.cands) do
			if #keep >= keepPerGroup + math.min(kinds.base or 0, baseScreen) + supplied then break end
			local dup = false
			for _, k in ipairs(keep) do if k == c then dup = true end end
			if not dup then t_insert(keep, c) end
		end
		table.sort(keep, function(a, b)
			-- items someone supplied are measured first
			if (a.entry.kind == "supplied") ~= (b.entry.kind == "supplied") then return a.entry.kind == "supplied" end
			if a.score ~= b.score then return a.score > b.score end
			return (a.entry.src.title or a.entry.src.name or "") < (b.entry.src.title or b.entry.src.name or "")
		end)
		g.cands = keep
	end
	out.counts.discovered = discovered
	table.sort(differentConfig, function(a, b)
		if a.score ~= b.score then return a.score > b.score end
		return a.name < b.name
	end)
	for i = #differentConfig, 13, -1 do differentConfig[i] = nil end
	out.different_configuration = differentConfig
	-- measurement order: empty / no-benefit targets first, then by best static score
	table.sort(groups, function(a, b)
		local da = (a.target.empty or (a.target.no_measured_benefit and not a.target.other_effects)) and 0 or 1
		local db = (b.target.empty or (b.target.no_measured_benefit and not b.target.other_effects)) and 0 or 1
		if da ~= db then return da < db end
		local sa, sb = (a.cands[1] or {}).score or 0, (b.cands[1] or {}).score or 0
		if sa ~= sb then return sa > sb end
		return a.kind < b.kind
	end)
	-- the budget is split: drop-in replacements, then crafted affixes (up to 9), then condition scenarios (up to 6)
	local craftReserve = math.min(9, math.floor(maxEvals / 6))
	local condReserve = math.min(6, math.floor(maxEvals / 8))
	local dropBudget = maxEvals - craftReserve - condReserve
	out.counts.drop_in_evals, out.counts.crafted_evals = 0, 0
	local function evalItem(target, item, crafted)
		out.counts.evaluated = out.counts.evaluated + 1
		if crafted then out.counts.crafted_evals = out.counts.crafted_evals + 1
		else out.counts.drop_in_evals = out.counts.drop_in_evals + 1 end
		resp.recalcs = (resp.recalcs or 0) + 1
		return Cand.diff(base, Cand.metrics(cf({ repSlotName = target, repItem = item })))
	end
	local results = {}
	local idx = {}
	local progressed = true
	while progressed and out.counts.evaluated < dropBudget do
		progressed = false
		for _, g in ipairs(groups) do
			idx[g] = (idx[g] or 0) + 1
			local c = g.cands[idx[g]]
			if c and out.counts.evaluated < dropBudget then
				progressed = true
				local it = c.entry.kind == "unique" and freshItem(c.entry.src:BuildRaw()) or c.entry.src
				c.eval = it and evalItem(g.target.slot, it) or nil
				c.item = it
			end
		end
	end
	-- magic-only bases (flask slot): the best measured base gets its best single affixes, then the pair
	local bestBase
	for _, g in ipairs(groups) do
		for _, c in ipairs(g.cands) do
			if c.eval and c.entry.kind == "base" and not c.eval.main_skill_no_damage then
				local v = (c.eval.dps_pct or 0) + (c.eval.ehp_pct or 0)
				if v > 0 and (not bestBase or v > bestBase.v) then bestBase = { v = v, c = c, g = g } end
			end
		end
	end
	local craftBudget = maxEvals - condReserve
	if bestBase and craftBudget - out.counts.evaluated >= 3 then
		local name = bestBase.c.entry.base_name
		local b = build.data.itemBases[name]
		local probe = new("Item", "Rarity: RARE\nProbe\n" .. name .. "\nItem Level: " .. ilvl)
		local affixes = {}
		for _, mod in pairs(probe.affixes or {}) do
			if (mod.type == "Prefix" or mod.type == "Suffix") and probe:GetModSpawnWeight(mod) > 0 and (mod.level or 1) <= ilvl then
				local key = mod.type .. "|" .. tostring(mod.group)
				if not affixes[key] or (mod.level or 1) > (affixes[key].level or 1) then affixes[key] = mod end
			end
		end
		local scored = { Prefix = {}, Suffix = {} }
		for _, mod in pairs(affixes) do
			local lines = modLines(mod)
			local it = freshItem(baseRaw(name, b, "MAGIC", lines))
			if it then
				local st = Cand.static(it, ctx)
				local gain = st.score - bestBase.c.st.score
				if gain > 0 then t_insert(scored[mod.type], { mod = mod, lines = lines, item = it, gain = gain }) end
			end
		end
		local chosen = {}
		out.crafted = { base = name, considered = 0, trials = {} }
		for _, ty in ipairs({ "Prefix", "Suffix" }) do out.crafted.considered = out.crafted.considered + #scored[ty] end
		for _, ty in ipairs({ "Prefix", "Suffix" }) do
			table.sort(scored[ty], function(x, y)
				if x.gain ~= y.gain then return x.gain > y.gain end
				return t_concat(x.lines, " ") < t_concat(y.lines, " ")
			end)
			local best
			for i = 1, math.min(4, #scored[ty]) do
				if craftBudget - out.counts.evaluated < 2 then break end
				local s = scored[ty][i]
				s.eval = evalItem(bestBase.g.target.slot, s.item, true)
				t_insert(out.crafted.trials, { type = ty, lines = s.lines, static_gain = round(s.gain, 2),
					dps_pct = s.eval.dps_pct, ehp_pct = s.eval.ehp_pct })
				if not best or (s.eval.dps_pct or 0) + (s.eval.ehp_pct or 0) > (best.eval.dps_pct or 0) + (best.eval.ehp_pct or 0) then best = s end
			end
			if best and (best.eval.dps_pct or 0) + (best.eval.ehp_pct or 0) > bestBase.v then chosen[ty] = best end
		end
		local lines = {}
		for _, ty in ipairs({ "Prefix", "Suffix" }) do if chosen[ty] then for _, l in ipairs(chosen[ty].lines) do t_insert(lines, l) end end end
		if #lines > 0 then
			local raw = baseRaw(name, b, "MAGIC", lines)
			local it = freshItem(raw)
			if it and out.counts.evaluated < craftBudget then
				local c = { entry = { kind = "magic", src = it, base_name = name, raw = raw }, st = Cand.static(it, ctx), crafted_from = name }
				c.score = c.st.score
				c.eval = evalItem(bestBase.g.target.slot, it, true)
				c.item = it
				t_insert(bestBase.g.cands, 1, c)
			end
		end
	end
	-- Rare affix templates for the weapon and amulet slots: the best legal affixes for this build on the equipped base
	-- (and the top bases of its class), tiers capped for the investment, the equipped item's resistance/attribute
	-- lines kept, measured as a drop-in like any unique. See references/candidates-rare-templates.md.
	do
		local rareSlots = opts.rare_slots or { "Weapon 1", "Amulet" }
		local rareEvalsTotal = math.max(0, tonumber(opts.rare_evals) or 40)
		if maxEvals <= 0 then rareEvalsTotal = 0 end   -- an item limit of 0 means no PoB evaluations at all
		out.counts.rare_evals = 0
		local slotIndex = 0
		local investment = lc(opts.investment or "medium")
		local topLimit = ({ league_starter = 1, league_start = 1, low = 1, budget = 2, medium = 3, high = 99 })[investment] or 3
		local charLevel = build.characterLevel or 90
		out.rare_templates = { investment = investment, top_tier_limit = topLimit, slots = {} }
		local function cheapGroup(group)
			return group and (group:find("Resist") or group:find("Strength") or group:find("Dexterity") or group:find("Intelligence")
				or group:find("Attribute") or group:find("Rarity")) and true or false
		end
		local isCI = (build.calcsTab.mainOutput and (tonumber(build.calcsTab.mainOutput.Life) or 0) <= 1)
		local function keepLine(text)
			local t = text:lower()
			return t:find("resistance") or t:find("to strength") or t:find("to dexterity") or t:find("to intelligence") or t:find("to all attributes")
				or t:find("to maximum life") or t:find("to maximum energy shield")
		end
		for _, slotName in ipairs(rareSlots) do
			if out.counts.rare_evals >= rareEvalsTotal then break end
			slotIndex = slotIndex + 1
			-- this slot's share of the affix-measurement budget: an even split, plus whatever earlier slots left
			local rareEvals = math.floor((rareEvalsTotal - out.counts.rare_evals) / (#rareSlots - slotIndex + 1)) + out.counts.rare_evals
			local g
			for _, gg in ipairs(groups) do
				for _, mem in ipairs(gg.members) do if mem.slot == slotName then g = gg end end
			end
			local slot, cur = candItemAt(slotName)
			if g and not locked[slotName] and (cur or slotName == "Amulet") then
				local slotReport = { slot = slotName, bases = {}, skipped = nil }
				-- 1. bases: the equipped base's type (a weapon class), or every amulet
				local curType = cur and cur.base and cur.base.type or (slotName == "Amulet" and "Amulet" or nil)
				local bases = {}
				if curType then
					for name, b in pairs(build.data.itemBases) do
						local lvlReq = b.req and b.req.level or 0
						if b.type == curType and lvlReq <= charLevel and not (b.subType and lc(b.subType):find("talisman"))
							and not lc(name):find("talisman") and not b.hidden then
							t_insert(bases, { name = name, b = b, lvl = lvlReq, equipped = cur and cur.baseName == name })
						end
					end
				end
				table.sort(bases, function(x, y)
					if (x.equipped and 1 or 0) ~= (y.equipped and 1 or 0) then return x.equipped end
					if x.lvl ~= y.lvl then return x.lvl > y.lvl end
					return x.name < y.name
				end)
				local maxBases = slotName == "Amulet" and 4 or 3
				while #bases > maxBases do table.remove(bases) end
				local slotMeasured = nil   -- affix measurements on the first base, reused by the others
				if #bases == 0 then slotReport.skipped = "no base of this class is usable at level " .. charLevel end
				-- the equipped item's resistance / attribute lines come along at their current values
				local keep = {}
				if cur and (cur.rarity == "RARE" or cur.rarity == "MAGIC") then
					for _, ml in ipairs(cur.explicitModLines or {}) do
						local text = cleanModLine(rolledLine(ml))
						if keepLine(text) then t_insert(keep, text) end
					end
				end
				for _, bs in ipairs(bases) do
					if out.counts.rare_evals >= rareEvalsTotal then break end
					-- 2. the legal affix pool at the item level: every tier per group, best first
					local probe = new("Item", "Rarity: RARE\nProbe\n" .. bs.name .. "\nItem Level: " .. ilvl)
					local tiers = {}
					for _, mod in pairs(probe.affixes or {}) do
						if (mod.type == "Prefix" or mod.type == "Suffix") and mod.group and probe:GetModSpawnWeight(mod) > 0 and (mod.level or 1) <= ilvl then
							local key = mod.type .. "|" .. mod.group
							tiers[key] = tiers[key] or { type = mod.type, group = mod.group, mods = {} }
							t_insert(tiers[key].mods, mod)
						end
					end
					for _, t in pairs(tiers) do table.sort(t.mods, function(a, b) return (a.level or 1) > (b.level or 1) end) end
					-- bench crafts for this base (one at most)
					local crafts = {}
					for _, mod in ipairs(build.data.masterMods) do
						if (mod.type == "Prefix" or mod.type == "Suffix") and mod.group and mod.types and mod.types[bs.b.type] then
							local key = mod.type .. "|" .. mod.group
							if not crafts[key] or (mod.level or 1) > (crafts[key].level or 1) then crafts[key] = mod end
						end
					end
					-- 3. kept lines first: find their type and group so they take their affix slots
					local chosen, usedGroups, count = {}, {}, { Prefix = 0, Suffix = 0 }
					for _, text in ipairs(keep) do
						local found
						for _, t in pairs(tiers) do
							for _, mod in ipairs(t.mods) do
								for _, tpl in ipairs(modLines(mod)) do
									local m, inRange = matchModLine(text, tpl)
									if m and inRange then found = { type = t.type, group = t.group } break end
								end
								if found then break end
							end
							if found then break end
						end
						if found and count[found.type] < 3 and not usedGroups[found.group] then
							usedGroups[found.group] = true
							count[found.type] = count[found.type] + 1
							t_insert(chosen, { lines = { text }, type = found.type, group = found.group, kept = true, tier = "kept" })
						end
					end
					-- a non-weapon slot on a life (or ES) build always carries its pool prefix: the static scorer values
					-- damage, so a template that let the life line go would always rate as a trade-off
					if slotName ~= "Weapon 1" and slotName ~= "Weapon 2" then
						local want = isCI and "to maximum energy shield" or "to maximum life"
						local have = false
						for _, c in ipairs(chosen) do for _, l in ipairs(c.lines) do if l:lower():find(want) then have = true end end end
						if not have and count.Prefix < 3 then
							local bestMod, bestKey
							for key, t in pairs(tiers) do
								if t.type == "Prefix" and not usedGroups[t.group] then
									local tpl = modLines(t.mods[1])[1] or ""
									if #modLines(t.mods[1]) == 1 and tpl:lower():find("^%+%(?%d") and tpl:lower():find(want) and (not bestMod or (t.mods[1].level or 1) > (bestMod.level or 1)) then
										bestMod, bestKey = t.mods[1], key
									end
								end
							end
							if bestMod then
								usedGroups[tiers[bestKey].group] = true
								count.Prefix = count.Prefix + 1
								t_insert(chosen, { lines = modLines(bestMod), type = "Prefix", group = tiers[bestKey].group, tierIdx = 1, tierCount = #tiers[bestKey].mods, mod = bestMod, pool = true })
							end
						end
					end
					-- 4. measured greedy. The static scorer cannot value gem levels, crit synergies or conversion, so each
					--    candidate affix is measured ONCE by PoB as a one-line addition to the kept lines on this base (its own
					--    budget, `rare_evals`), and the template is assembled greedily from those measured gains, one mod per
					--    group, at most one bench craft. Bases after the first reuse the first base's measurements.
					local function itemWith(list)
						local lines = {}
						for _, c in ipairs(list) do for _, l in ipairs(c.lines) do t_insert(lines, (c.crafted and "{crafted}" or "") .. l) end end
						return freshItem(baseRaw(bs.name, bs.b, "RARE", lines, ilvl))
					end
					local function measure(it)
						if out.counts.rare_evals >= rareEvals then return nil end
						out.counts.rare_evals = out.counts.rare_evals + 1
						resp.recalcs = (resp.recalcs or 0) + 1
						local d = Cand.diff(base, Cand.metrics(cf({ repSlotName = g.target.slot, repItem = it })))
						return (d.dps_pct or 0) + (d.ehp_pct or 0), d
					end
					local keptItem = itemWith(chosen)
					local keptGain = keptItem and measure(keptItem) or 0
					local trials = 0
					if not slotMeasured then
						-- candidates: every pool affix (best tier) and bench craft, static-prefiltered, gem levels always in
						local cands = {}
						for _, t in pairs(tiers) do
							if not usedGroups[t.group] then t_insert(cands, { lines = modLines(t.mods[1]), type = t.type, group = t.group, tierIdx = 1, tierCount = #t.mods, mod = t.mods[1], key = t.type .. "|" .. t.group }) end
						end
						for key, mod in pairs(crafts) do
							if not usedGroups[mod.group] then t_insert(cands, { lines = modLines(mod), type = mod.type, group = mod.group, crafted = true, tierIdx = 1, tierCount = 1, mod = mod, key = key .. "|craft" }) end
						end
						local keptStatic = keptItem and Cand.static(keptItem, ctx).score or 0
						-- a "+N to Level of all <Tag> Skill Gems" line helps only if the main gem carries that tag
						local gemTags = (ms.activeEffect and ms.activeEffect.gemData and ms.activeEffect.gemData.tags) or {}
						local function gemLevelApplies(text)
							local t = text:lower()
							if not t:find("level of") then return nil end
							if t:find("socketed") then return true end
							local tag = t:match("level of all (%a+) spell skill gems") or t:match("level of all (%a+) skill gems")
							if not tag then return true end
							if tag == "spell" then return gemTags.spell and true or false end
							return gemTags[tag] and true or false
						end
						for _, c in ipairs(cands) do
							local text = t_concat(c.lines, " ")
							local gl = gemLevelApplies(text)
							c.gemLevel = gl == true
							c.skip = gl == false
							if not c.skip then
								local trial = {}
								for _, k in ipairs(chosen) do t_insert(trial, k) end
								t_insert(trial, c)
								local it = itemWith(trial)
								c.static = it and (Cand.static(it, ctx).score - keptStatic) or -1
								c.item = it
							else
								c.static = -1
							end
						end
						table.sort(cands, function(x, y)
							if x.gemLevel ~= y.gemLevel then return x.gemLevel end
							if x.static ~= y.static then return x.static > y.static end
							return t_concat(x.lines, " ") < t_concat(y.lines, " ")
						end)
						slotMeasured = {}
						for _, c in ipairs(cands) do
							if out.counts.rare_evals >= rareEvals - #bases then break end
							if c.item and not c.skip and (c.static > 0.02 or c.gemLevel) then
								trials = trials + 1
								local gain = measure(c.item)
								if gain then c.gain = gain - keptGain slotMeasured[c.key] = c end
							end
						end
					end
					-- assemble: best measured gain first, one mod per group, counts respected, one bench craft
					local craftedUsed = false
					local list = {}
					for _, c in pairs(slotMeasured) do t_insert(list, c) end
					table.sort(list, function(x, y) if (x.gain or 0) ~= (y.gain or 0) then return (x.gain or 0) > (y.gain or 0) end return t_concat(x.lines, " ") < t_concat(y.lines, " ") end)
					for _, c in ipairs(list) do
						if (c.gain or 0) > (c.crafted and 1.0 or 0.3) and count[c.type] < 3 and not usedGroups[c.group] and not (c.crafted and craftedUsed) then
							local pick = { lines = c.lines, type = c.type, group = c.group, crafted = c.crafted, tierIdx = c.tierIdx, tierCount = c.tierCount, mod = c.mod, gain = c.gain }
							t_insert(chosen, pick)
							usedGroups[c.group] = true
							count[c.type] = count[c.type] + 1
							if c.crafted then craftedUsed = true end
						end
					end
					-- 5. tier caps: only `topLimit` costly groups keep the best tier; the rest step down one tier
					local costly = {}
					for _, c in ipairs(chosen) do
						if not c.kept and not c.crafted and not cheapGroup(c.group) and c.tierCount and c.tierCount > 1 then t_insert(costly, c) end
					end
					table.sort(costly, function(a, b) return (a.gain or 0) > (b.gain or 0) end)
					local demoted = 0
					for i = topLimit + 1, #costly do
						local c = costly[i]
						local key = c.type .. "|" .. c.group
						local second = tiers[key] and tiers[key].mods[2]
						if second then c.lines = modLines(second) c.mod = second c.tierIdx = 2 demoted = demoted + 1 end
					end
					local final = itemWith(chosen)
					local lineReport = {}
					for _, c in ipairs(chosen) do
						t_insert(lineReport, { text = t_concat((function() local r = {} for _, l in ipairs(c.lines) do t_insert(r, midRoll(l)) end return r end)(), " / "),
							type = c.type, tier = c.kept and "kept" or (c.pool and "pool, T1" or (c.crafted and "bench craft" or ("T" .. tostring(c.tierIdx) .. (cheapGroup(c.group) and "" or "*")))),
							static_gain = c.gain and round(c.gain, 2) or nil })
					end
					local entry = { base = bs.name, equipped_base = bs.equipped or nil, lines = lineReport, kept = #keep, demoted = demoted, trials = trials }
					-- the final measurement is a rare-template evaluation too: it counts against --rare-evals, not the item limit
					if final and #chosen > 0 and out.counts.rare_evals < rareEvals and out.counts.rare_evals < rareEvalsTotal then
						local raw = baseRaw(bs.name, bs.b, "RARE", (function() local r = {} for _, c in ipairs(chosen) do for _, l in ipairs(c.lines) do t_insert(r, (c.crafted and "{crafted}" or "") .. l) end end return r end)(), ilvl)
						local c = { entry = { kind = "rare", src = final, base_name = bs.name, raw = raw }, st = Cand.static(final, ctx), crafted_from = bs.name, template = entry }
						c.score = c.st.score
						out.counts.rare_evals = out.counts.rare_evals + 1
						resp.recalcs = (resp.recalcs or 0) + 1
						c.eval = Cand.diff(base, Cand.metrics(cf({ repSlotName = g.target.slot, repItem = final })))
						c.item = final
						entry.eval = c.eval
						t_insert(g.cands, 1, c)
					end
					t_insert(slotReport.bases, entry)
				end
				t_insert(out.rare_templates.slots, slotReport)
			end
		end
	end
	-- condition scenarios: for measured candidates whose lines need a condition the build has a
	-- source for, the same comparison with that condition configured. The config change and the item
	-- change are reported separately.
	local scen = {}
	for _, g in ipairs(groups) do
		for _, c in ipairs(g.cands) do
			if c.eval and #c.st.conditions > 0 then
				local vars, seen = {}, {}
				for _, d in ipairs(c.st.conditions) do
					if d.config and d.evidence == "source found" and d.sustaining_source and not d.negated and not seen[d.config] then
						seen[d.config] = true
						t_insert(vars, d.config)
					end
				end
				if #vars > 0 then
					table.sort(vars)
					local key = t_concat(vars, ",")
					scen[key] = scen[key] or { vars = vars, list = {} }
					t_insert(scen[key].list, { c = c, g = g })
				end
			end
		end
	end
	local ct = build.configTab
	local nScen = 0
	local scenList = {}
	for key, sc in pairs(scen) do
		sc.key, sc.value = key, -math.huge
		for _, e in ipairs(sc.list) do
			sc.value = math.max(sc.value, e.c.eval.dps_pct or 0)
		end
		table.sort(sc.list, function(a, b) return (a.c.eval.dps_pct or 0) > (b.c.eval.dps_pct or 0) end)
		t_insert(scenList, sc)
	end
	table.sort(scenList, function(a, b) if a.value ~= b.value then return a.value > b.value end return a.key < b.key end)
	for _, s in ipairs(scenList) do
		if nScen >= 2 or maxEvals - out.counts.evaluated < 2 then break end
		nScen = nScen + 1
		local saved = {}
		for _, v in ipairs(s.vars) do saved[v] = ct.input[v]; ct.input[v] = true end
		measure()
		local cf2 = build.calcsTab:GetMiscCalculator()
		local base2 = Cand.metrics(cf2({}))
		out.counts.conditional_evals = out.counts.conditional_evals + 1
		out.counts.evaluated = out.counts.evaluated + 1
		for i, e in ipairs(s.list) do
			if i > 3 or out.counts.evaluated >= maxEvals then break end
			out.counts.evaluated = out.counts.evaluated + 1
			out.counts.conditional_evals = out.counts.conditional_evals + 1
			local m = Cand.metrics(cf2({ repSlotName = e.g.target.slot, repItem = e.c.item }))
			e.c.conditional = { config = s.vars, config_effect = Cand.diff(base, base2), item_effect = Cand.diff(base2, m) }
		end
		for _, v in ipairs(s.vars) do ct.input[v] = saved[v] end
		measure()
	end
	-- passives that would enable a candidate: they name its item type, or supply a condition it needs
	local wantTypes, wantVars, seenVar = {}, {}, {}
	for _, g in ipairs(groups) do
		for _, c in ipairs(g.cands) do
			if c.eval and ((c.eval.dps_pct or 0) >= 1 or (c.eval.ehp_pct or 0) >= 1) and (c.eval.min_ele_res_change or 0) >= 0 then
				wantTypes[c.entry.src.type or ""] = true
				for _, d in ipairs(c.st.conditions) do
					if d.config and not seenVar[d.config] then seenVar[d.config] = true t_insert(wantVars, d.config) end
				end
			end
		end
	end
	for _, d in ipairs(differentConfig) do wantTypes[d.type or ""] = true end
	local enablers, seenNode = {}, {}
	for _, n in ipairs(mentions.nodes) do
		if wantTypes[n.item_type] and not seenNode[n.id] then
			seenNode[n.id] = true
			n.reason = "names " .. n.item_type .. " items, which are among the promising or different-configuration candidates"
			t_insert(enablers, n)
		end
	end
	for _, n in ipairs(Cand.conditionNodes(wantVars)) do
		if not seenNode[n.id] then
			seenNode[n.id] = true
			n.reason = "supplies " .. n.supplies .. ", which a candidate needs"
			t_insert(enablers, n)
		end
	end
	table.sort(enablers, function(a, b)
		if (a.ascendancy ~= nil) ~= (b.ascendancy ~= nil) then return a.ascendancy ~= nil end
		return (a.points or 99) < (b.points or 99)
	end)
	for i = #enablers, 11, -1 do enablers[i] = nil end
	local ascUsed = 0
	for id, node in pairs(build.spec.allocNodes) do
		if node.ascendancyName and node.type ~= "AscendClassStart" and not node.isMultipleChoiceOption then ascUsed = ascUsed + 1 end
	end
	out.ascendancy_points = { used = ascUsed, available = ascPointsAvailable(level) }
	out.enablers = enablers
	-- serialise
	out.groups = {}
	for _, g in ipairs(groups) do
		local og = { kind = g.kind, target = g.target.slot, target_item = g.target.name, target_empty = g.target.empty or nil,
			target_no_measured_benefit = g.target.no_measured_benefit, target_other_effects = g.target.other_effects,
			target_utility = g.target.without and g.target.without.utility_changed or nil,
			discovered = g.discovered, candidates = {} }
		for _, c in ipairs(g.cands) do
			local it = c.item or c.entry.src
			local lim = {}
			local b = it.base or {}
			if b.flask or b.tincture then
				local fd = b.flask or b.tincture
				local facts = {}
				for k, v in pairs(fd) do if type(v) == "number" then t_insert(facts, k .. " " .. tostring(v)) end end
				table.sort(facts)
				t_insert(lim, "PoB counts an active flask-slot item as always on; its duration, charges, cooldown and any cost it puts on you are not simulated (base data: " .. t_concat(facts, ", ") .. ")")
			end
			if #c.st.conditions > 0 then t_insert(lim, "needs a condition whose uptime PoB does not model") end
			if c.st.unsupported > 0 then t_insert(lim, c.st.unsupported .. " line(s) PoB does not calculate") end
			if out.profile.part and out.profile.part.count > 1 then
				t_insert(lim, "measured on the configured skill part '" .. tostring(out.profile.part.name) .. "' only")
			end
			local specItem
			if c.entry.kind == "unique" then specItem = { slot = g.target.slot, unique = it.title }
			elseif c.entry.kind == "supplied" then specItem = { slot = g.target.slot, text = c.entry.raw }
			else specItem = { slot = g.target.slot, text = c.entry.raw } end
			t_insert(og.candidates, {
				name = it.title or it.name, kind = c.entry.kind, base = it.baseName, type = it.type, crafted_from = c.crafted_from,
				static_score = round(c.score or 0, 2), lines = c.st.lines, conditions = c.st.conditions,
				conditions_supported = c.st.conditions_supported, named_by_passives = c.named,
				status = c.eval and "evaluated" or "discovered", eval = c.eval, conditional = c.conditional,
				limitations = lim, source = it.source, league = it.league, req_level = it.requirements and it.requirements.level,
				spec_item = specItem, changes_weapon_class = c.changes_weapon_class, template = c.template })
		end
		t_insert(out.groups, og)
	end
	out.counts.seconds = round(os.clock() - t0, 2)
	out.counts.static_kept = 0
	for _, g in ipairs(out.groups) do out.counts.static_kept = out.counts.static_kept + #g.candidates end
	return out
end

---------------------------------------------------------------------------
-- Point filler: spend the unspent passive points on the nodes PoB values most
---------------------------------------------------------------------------
local function fillCandidateOK(node)
	if node.alloc or not node.path or #node.path == 0 then return false end
	if node.ascendancyName or node.isProxy or node.isMultipleChoiceOption or node.isBlighted then return false end
	if node.type ~= "Normal" and node.type ~= "Notable" then return false end   -- no keystones, masteries, sockets
	if node.expansionJewel or node.isJewelSocket then return false end
	for _, p in ipairs(node.path) do
		if p ~= node and not p.alloc and (p.type ~= "Normal" and p.type ~= "Notable") then return false end
		if p.ascendancyName or p.isProxy then return false end
	end
	return true
end

-- Cheap prefilter with PoB's own modifier matching: a node is worth measuring only if at least one
-- of its modifiers can apply to the main skill (generic mods such as life always can).
local function fillModApplies(mod, ctx)
	if not cfgApplies(ctx.cfg, mod) then return false end
	if mod.name == "MinionModifier" and not (ctx.ms.skillFlags and ctx.ms.skillFlags.minion) then return false end
	for _, tag in ipairs(mod) do
		local t = tag.type
		if t == "SkillType" or t == "SkillName" or t == "SkillId" then
			local ok, state = pcall(Cand.tagState, tag, ctx)
			if ok and state ~= "ok" then return false end
		end
	end
	return true
end

local function fillRelevant(node, ctx)
	if not ctx or not node.modList then return true end
	for _, mod in ipairs(node.modList) do
		if fillModApplies(mod, ctx) then return true end
	end
	return false
end

local function fillKey(node)
	local parts = {}
	for _, p in ipairs(node.path) do
		if not p.alloc then t_insert(parts, p.id == node.id and (t_concat(p.sd or {}, "/")) or ("via:" .. t_concat(p.sd or {}, "/"))) end
	end
	return t_concat(parts, " | ")
end

local function fillSummary(o)
	local function fin(v) v = tonumber(v) or 0 if v ~= v or v == math.huge then return 0 end return v end
	return { dps = math.max(tonumber(o.FullDPS) or 0, tonumber(o.CombinedDPS) or 0), life = o.Life or 0, es = o.EnergyShield or 0,
		min_ele_res = math.min(o.FireResist or 0, o.ColdResist or 0, o.LightningResist or 0),
		-- weakest max hit over the four hit types (chaos excluded: CI builds report it as infinite)
		min_max_hit = math.min(fin(o.PhysicalMaximumHitTaken), fin(o.FireMaximumHitTaken), fin(o.ColdMaximumHitTaken), fin(o.LightningMaximumHitTaken)),
		-- share of incoming hits that land, per class of hit: attacks (evade, attack block), spells (suppression halves,
		-- spell block). Continuous, so avoidance nodes and masteries count; PoB's stepped TotalEHP is still not used.
		hits_attack = (1 - math.min(fin(o.MeleeEvadeChance), 100) / 100) * (1 - math.min(fin(o.EffectiveBlockChance), 100) / 100),
		hits_spell = (1 - math.min(fin(o.EffectiveSpellSuppressionChance), 100) / 100 * 0.5 * (1 + fin(o.SpellSuppressionEffect or 0) / 100))
			* (1 - math.min(fin(o.EffectiveSpellBlockChance), 100) / 100),
		recovery = fin(o.LifeLeechGainRate) + fin(o.LifeRegenRecovery) + fin(o.LifeOnHitRate),
		-- PoB's effective hit pool: the pool against the configured mix of hit types after every mitigation
		-- layer (armour, charges, taken-as, resistances) and avoidance; broad mitigation (an endurance
		-- charge, phys taken as fire) moves it where the single weakest max hit barely moves
		ehp = fin(o.TotalEHP) }
end

-- Score = weighted DPS% + defence%, where defence combines PoB's effective hit pool % (pool, every
-- mitigation layer and avoidance against the configured hit mix), the weakest-max-hit % (so a layer
-- that only helps the common hit types can't hide a one-shot), avoidance % and recovery %.
-- The pool alone (life+ES) is not a term: it is inside both EHP and the max hits, and scoring it
-- separately made a small life node outrank +1 endurance charge or a taken-as implicit.
local function fillScore(m, base, weights)
	local dpsPct = base.dps > 0 and (m.dps / base.dps - 1) * 100 or 0
	local baseDef = base.life + base.es
	local poolPct = baseDef > 0 and ((m.life + m.es) / baseDef - 1) * 100 or 0
	local hitPct = base.min_max_hit > 0 and (m.min_max_hit / base.min_max_hit - 1) * 100 or 0
	-- avoidance: fewer hits landing is worth the same as a bigger pool, so it enters as a % like the others
	local function fewer(a, b) return (a > 0 and b > 0) and (1 - b / a) * 100 or 0 end
	local avoidPct = (fewer(base.hits_attack, m.hits_attack) + fewer(base.hits_spell, m.hits_spell)) / 2
	-- recovery: leech, regen and on-hit per second, relative to the pool; capped so it can't dominate
	local baseRec = base.recovery or 0
	local recPct = (baseRec > 0 and base.life > 0) and math.max(-20, math.min(20, ((m.recovery or 0) - baseRec) / math.max(base.life, 1) * 100)) or 0
	local ehpPct = (base.ehp or 0) > 0 and ((m.ehp or 0) / base.ehp - 1) * 100 or poolPct
	local defPct = (ehpPct + hitPct + avoidPct + recPct) / 3
	local penalty = 0
	if m.min_ele_res < math.min(base.min_ele_res, 75) then penalty = penalty - 50 end
	return { score = weights.dps * dpsPct + weights.def * defPct + penalty, dps_pct = round(dpsPct, 2), def_pct = round(defPct, 2),
		pool_pct = round(poolPct, 2), ehp_pct = round(ehpPct, 2), hit_pct = round(hitPct, 2), avoid_pct = round(avoidPct, 2), recovery_pct = round(recPct, 2) }
end

-- Config conditions a candidate node would switch on: boolean options whose keyword the node's own stat
-- text generates in a sustaining way (same rules as the audit's evidence), and which are currently off.
local FILL_CONDITION_VARS = { "useFrenzyCharges", "usePowerCharges", "useEnduranceCharges", "buffOnslaught", "buffFortification",
	"buffElusive", "buffUnholyMight", "buffPhasing", "buffTailwind", "buffAdrenaline", "conditionEnemyIntimidated",
	"conditionEnemyBlinded", "conditionEnemyMaimed", "conditionEnemyHindered", "conditionEnemyUnnerved", "conditionEnemyCursed",
	"conditionEnemyShocked", "conditionEnemyChilled", "conditionEnemyIgnited", "conditionEnemyScorched", "conditionEnemyBrittle",
	"conditionEnemySapped", "conditionEnemyCoveredInAsh", "conditionEnemyBleeding", "conditionEnemyPoisoned" }

-- Numeric config options the fill can set when a node grants a sustaining source for them: Rage from
-- "Gain N Rage on (Melee) Hit" in a melee build sets multiplierRage to PoB's maximum (the hits are
-- frequent enough to keep it capped). Each entry: var, the stat-text pattern, a guard, and the value.
local function fillMainSkillIsMelee()
	local ms = build.calcsTab and build.calcsTab.mainEnv and build.calcsTab.mainEnv.player and build.calcsTab.mainEnv.player.mainSkill
	local flags = ms and ms.skillFlags or {}
	local types = ms and ms.skillTypes or {}
	-- the gem's own type, not the selected part's flags: a strike skill's projectile part is still a melee hit
	return flags.attack and (flags.melee or types[SkillType.Melee]) and true or false
end
local FILL_NUMERIC_VARS = {
	{ var = "multiplierRage", patterns = { "gain %d+ rage on melee hit", "gain %d+ rage on hit", "gain %d+ rage when you hit" },
	  guard = fillMainSkillIsMelee, value = function() return (build.calcsTab.mainOutput and build.calcsTab.mainOutput.MaximumRage) or 30 end },
	-- Withered stacks (max 15) from a sustaining source (Withering Presence, Withering Touch, Withering Step) in a chaos build
	{ var = "multiplierWitheredStackCount", patterns = { "wither" }, value = function() return 15 end,
	  guard = function()
		local o = build.calcsTab.mainOutput or {}
		for _, out in ipairs({ o, o.MainHand or {}, o.OffHand or {} }) do if (tonumber(out.ChaosHitAverage) or 0) > 0 then return true end end
		return (tonumber(o.ChaosDot) or 0) > 0 or (tonumber(o.TotalDotDPS) or 0) > 0
	  end },
	-- High-Impact Mine's aura: 2% more damage per mine detonated recently, up to 10 (honest while chain-detonating)
	{ var = "multiplierMineDetonatedRecently", patterns = { "high%-impact mine" }, value = function() return 10 end,
	  guard = function()
		local ms = build.calcsTab and build.calcsTab.mainEnv and build.calcsTab.mainEnv.player and build.calcsTab.mainEnv.player.mainSkill
		return ms and ms.skillFlags and ms.skillFlags.mine and true or false
	  end },
	-- Herald of Agony: Virulence at its cap of 40 while the build keeps poisoning
	{ var = "heraldOfAgonyVirulenceStack", patterns = { "virulence" }, guard = function() return true end, value = function() return 40 end },
}

-- Returns a list of { var = <config option>, value = <value to set> } for conditions the node's stat text
-- sustains and the build has not configured yet (boolean vars get true; numeric vars their listed value).
-- An ailment condition needs the element behind it: the main skill (its configured part) must deal that
-- element. "10% chance to Ignite" in a pure lightning build is no source.
local FILL_AILMENT_ELEMENT = { conditionEnemyIgnited = "Fire", conditionEnemyScorched = "Fire", conditionEnemyChilled = "Cold",
	conditionEnemyFrozen = "Cold", conditionEnemyBrittle = "Cold", conditionEnemyShocked = "Lightning", conditionEnemySapped = "Lightning" }
local function fillDealsElement(element)
	local o = build.calcsTab.mainOutput or {}
	-- the main skill's hit only: a guard or utility skill that is typed Fire (Molten Shell) is not a source of ignites
	-- attacks keep per-weapon figures under MainHand/OffHand; spells on the output itself
	for _, out in ipairs({ o, o.MainHand or {}, o.OffHand or {} }) do
		if (tonumber(out[element .. "HitAverage"]) or 0) > 0 then return true end
	end
	return false
end

local function fillConditionSources(sdLines)
	local out = {}
	local input = build.configTab.input
	for _, var in ipairs(FILL_CONDITION_VARS) do
		if not input[var] and not (FILL_AILMENT_ELEMENT[var] and not fillDealsElement(FILL_AILMENT_ELEMENT[var])) then
			local kws = keywordsFor(var)
			local found = false
			for _, kw in ipairs(kws or {}) do
				for _, text in ipairs(sdLines or {}) do
					if lineIsSource(text, kw) then
						local _, sustaining = triggerOf("passive", text)
						if sustaining then found = true break end
					end
				end
				if found then break end
			end
			if found then t_insert(out, { var = var, value = true }) end
		end
	end
	for _, nv in ipairs(FILL_NUMERIC_VARS) do
		if not (tonumber(input[nv.var]) and tonumber(input[nv.var]) > 0) and nv.guard() then
			local found = false
			for _, text in ipairs(sdLines or {}) do
				local t = text:lower()
				for _, pat in ipairs(nv.patterns) do
					if t:find(pat) then
						local _, sustaining = triggerOf("passive", text)
						if sustaining then found = true end
						break
					end
				end
				if found then break end
			end
			if found then t_insert(out, { var = nv.var, value = nv.value() }) end
		end
	end
	return out
end

-- Run `fn(calcFuncWithConditions)` with the given config conditions switched on, then restore them.
-- The node's value is measured against the baseline WITHOUT the condition: without the node there is no source.
local function fillWithConditions(vars, fn)
	local input = build.configTab.input
	local saved = {}
	for _, c in ipairs(vars) do saved[c.var] = input[c.var] input[c.var] = c.value end
	build.configTab:BuildModList()
	local cf = build.calcsTab.calcs.getMiscCalculator(build)
	local ok, res = pcall(fn, cf)
	for _, c in ipairs(vars) do input[c.var] = saved[c.var] end
	build.configTab:BuildModList()
	if not ok then return nil end
	return res
end

-- A mastery effect measured on a mastery node one step from the tree: the node temporarily carries
-- that effect's stats (as PoB does once it is selected), is measured as an added node, then restored.
local function fillMeasureMastery(calcFunc, spec, node, effectId, base, weights, report)
	local effect = spec.tree.masteryEffects[effectId]
	if not effect then return nil end
	node.sd = effect.sd
	node.allMasteryOptions = false
	spec.tree:ProcessStats(node)
	node.finalModList = node.modList   -- the calculation reads finalModList (set by the last full calc), not modList
	local conds = fillConditionSources(effect.sd)
	local ok, m
	if #conds > 0 then
		m = fillWithConditions(conds, function(cf) return fillSummary(cf({ addNodes = { [node] = true } }, true)) end)
		if report then report.recalcs = report.recalcs + 2 end
		ok = m ~= nil
	else
		ok, m = pcall(function() return fillSummary(calcFunc({ addNodes = { [node] = true } }, true)) end)
	end
	spec:AddMasteryEffectOptionsToNode(node)
	node.finalModList = node.modList
	if not ok or not m then return nil end
	local sc = fillScore(m, base, weights)
	if #conds > 0 then sc.enables = conds end
	sc.text = t_concat(effect.sd or {}, " | "):lower()
	return sc
end

-- Measure a node (and the path to it) as a drop-in on the current tree with PoB's own
-- calculator for tree changes (the one its tree-power display uses): one calculation in
-- CALCULATOR mode with the nodes added, without allocating them and without the breakdown pass
-- a full recalculation also does.
local function fillMeasure(calcFunc, node, base, weights, report)
	local add = {}
	local sd = {}
	for _, p in ipairs(node.path) do
		if not p.alloc then
			add[p] = true
			for _, l in ipairs(p.sd or {}) do t_insert(sd, l) end
		end
	end
	local conds = fillConditionSources(sd)
	local m
	if #conds > 0 then
		m = fillWithConditions(conds, function(cf) return fillSummary(cf({ addNodes = add }, true)) end)
		if report then report.recalcs = report.recalcs + 2 end
	end
	if not m then m = fillSummary(calcFunc({ addNodes = add }, true)) conds = {} end
	local sc = fillScore(m, base, weights)
	if #conds > 0 then sc.enables = conds end
	sc.text = t_concat(sd, " | "):lower()
	return sc
end

-- A config option the fill just switched on changes what other candidates are worth: "+1 to Maximum
-- Frenzy Charges" scored nothing while useFrenzyCharges was off. Cached scores whose stat text names
-- the option's keyword are dropped so those candidates are measured again on the next pass (lazy
-- re-verification alone would never reach them: a stale zero never becomes the winner).
local function fillInvalidateFor(scores, var)
	local kws = keywordsFor(var) or {}
	local dropped = 0
	for key, sc in pairs(scores) do
		if sc.text then
			for _, kw in ipairs(kws) do
				local pat = type(kw) == "table" and kw[1] or kw
				if sc.text:find(pat) then scores[key] = nil dropped = dropped + 1 break end
			end
		end
	end
	return dropped
end

-- Greedy, measured: each distinct node kind (its stat text, plus the stat text of the unallocated
-- nodes on the way to it) is measured once per run with a PoB recalculation against the starting
-- tree; the best value per point is allocated, until the points are spent, nothing has a positive
-- value, or the recalc budget is used up. Small nodes are only considered on the frontier (one
-- point away); notables up to max_cost points away. Scores are not re-measured after each
-- allocation, so this is a heuristic, not an optimum: the report shows what each node was worth.
local function fillTree(level, extraPoints)
	local spec = build.spec
	local opt = FILL
	local used = spec:CountAllocNodes()
	local avail = pointsAvailable(level, extraPoints)
	local remaining = avail - used - opt.reserve
	local report = { objective = opt.objective, points_before = used, points_available = avail, added = {}, recalcs = 0,
		points_added = 0, stopped = nil, masteries = {}, enabled_config = {} }
	if remaining <= 0 then
		report.stopped = "no unspent points"
		resp.fill = report
		return
	end
	local calcs = build.calcsTab.calcs
	local calcFunc, baseOut = calcs.getMiscCalculator(build)
	local base = fillSummary(baseOut)
	local env = build.calcsTab.mainEnv
	local ms = env and env.player and env.player.mainSkill
	local ctx = ms and ms.skillCfg and { env = env, ms = ms, cfg = ms.skillCfg } or nil
	if os.getenv("POE1_BUILDS_FILL_TRACE") then
		local tf = io.open(os.getenv("POE1_BUILDS_FILL_TRACE"), "a")
		for _, node in pairs(spec.nodes) do
			if node.type == "Mastery" and not node.alloc and node.masteryEffects and not node.ascendancyName and node.path and #node.path <= 3 then
				tf:write(("[fill] mastery-node %s id=%d pathlen=%d selected=%s\n"):format(node.dn or "?", node.id, #node.path, tostring(spec.masterySelections[node.id])))
			end
		end
		tf:close()
	end
	local scores = {}
	local relevant = {}
	local generation = 0   -- bumped on every allocation; a cached score from an older generation is re-measured before it wins
	while remaining > 0 do
		local best, bestVal, bestScore, bestEffect, bestKey, bestCost
		-- mastery effects already selected elsewhere on the tree can't be taken again
		local usedEffects = {}
		for _, eff in pairs(spec.masterySelections) do usedEffects[eff] = true end
		for _, node in pairs(spec.nodes) do
			if node.type == "Mastery" and not node.alloc and not spec.masterySelections[node.id] and node.path and #node.path == 1
				and not node.ascendancyName and not node.isProxy and node.masteryEffects and remaining >= 1 then
				for _, eff in ipairs(node.masteryEffects) do
					if not usedEffects[eff.effect] then
						local key = "mastery:" .. node.id .. ":" .. eff.effect
						local sc = scores[key]
						if not sc then
							if report.recalcs >= opt.max_recalcs then
								sc = { score = -math.huge, dps_pct = 0, def_pct = 0, unmeasured = true }
							else
								sc = fillMeasureMastery(calcFunc, spec, node, eff.effect, base, opt.weights, report) or { score = -math.huge, dps_pct = 0, def_pct = 0 }
								sc.generation = generation
								if os.getenv("POE1_BUILDS_FILL_TRACE") then local tf = io.open(os.getenv("POE1_BUILDS_FILL_TRACE"), "a") tf:write(("[fill] mastery %s | %s | score=%.2f dps=%s def=%s\n"):format(node.dn or "?", t_concat((spec.tree.masteryEffects[eff.effect] or {}).sd or {}, " / "), sc.score, tostring(sc.dps_pct), tostring(sc.def_pct))) tf:close() end
								report.recalcs = report.recalcs + 1
							end
							scores[key] = sc
						end
						if sc.score > 0.01 and (not bestScore or sc.score > bestScore
							or (sc.score == bestScore and ((node.dn or "") < (best.dn or "")
								or ((node.dn or "") == (best.dn or "") and node.id < best.id)))) then
							best, bestVal, bestScore, bestEffect, bestKey, bestCost = node, sc, sc.score, eff.effect, key, 1
						end
					end
				end
			end
			if relevant[node.id] == nil then relevant[node.id] = fillRelevant(node, ctx) end
			if relevant[node.id] and fillCandidateOK(node) then
				local cost = 0
				for _, p in ipairs(node.path) do if not p.alloc then cost = cost + 1 end end
				-- small nodes only on the frontier (cost 1); notables may be up to max_cost away
				local maxCost = node.type == "Notable" and opt.max_cost or 1
				if cost >= 1 and cost <= remaining and cost <= maxCost then
					-- a notable is measured once (by identity), a small node once per stat text
					local key = node.type == "Notable" and ("id:" .. node.id .. "/" .. cost) or fillKey(node)
					local s = scores[key]
					if not s then
						if report.recalcs >= opt.max_recalcs then
							s = { score = -math.huge, dps_pct = 0, def_pct = 0, unmeasured = true }
						else
							s = fillMeasure(calcFunc, node, base, opt.weights, report)
							s.generation = generation
							report.recalcs = report.recalcs + 1
							if os.getenv("POE1_BUILDS_FILL_TRACE") then local tf = io.open(os.getenv("POE1_BUILDS_FILL_TRACE"), "a") tf:write(("[fill] measure %s cost %d key=%s score=%.2f dps=%s def=%s base=%s/%s\n"):format(node.dn or "?", cost, key, s.score, tostring(s.dps_pct), tostring(s.def_pct), tostring(base.dps), tostring(base.life))) tf:close() end
						end
						scores[key] = s
					end
					local perPoint = s.score / cost
					-- deterministic order on ties: name, then node id (never Lua's hash order)
					if s.score > 0.01 and (not bestScore or perPoint > bestScore
						or (perPoint == bestScore and ((node.dn or "") < (best.dn or "")
							or ((node.dn or "") == (best.dn or "") and node.id < best.id)))) then
						best, bestVal, bestScore, bestEffect, bestKey, bestCost = node, s, perPoint, nil, key, cost
					end
				end
			end
		end
		if not best then
			report.stopped = report.recalcs >= opt.max_recalcs and "recalculation budget used up" or "no remaining node adds measurable value"
			break
		end
		-- lazy re-verification: a winner scored against an older tree is re-measured on the current one; if it
		-- no longer wins by its own fresh score, the loop picks again (caps, breakpoints and diminishing returns)
		if bestVal.generation ~= generation and report.recalcs < opt.max_recalcs then
			local fresh = bestEffect and fillMeasureMastery(calcFunc, spec, best, bestEffect, base, opt.weights, report)
				or (not bestEffect and fillMeasure(calcFunc, best, base, opt.weights, report)) or nil
			report.recalcs = report.recalcs + 1
			if fresh then
				fresh.generation = generation
				scores[bestKey] = fresh
				local freshPer = fresh.score / (bestCost or 1)
				if fresh.score <= 0.01 or freshPer < bestScore * 0.999 then
					report.reverified = (report.reverified or 0) + 1
					goto continue_loop   -- re-run the selection with the corrected score
				end
				bestVal = fresh
			end
		end
		local cost = 0
		local via, ids = {}, {}
		for _, p in ipairs(best.path) do
			if not p.alloc then
				cost = cost + 1
				t_insert(ids, p.id)
				if p ~= best then t_insert(via, p.dn or "?") end
			end
		end
		local effectText
		if bestEffect then
			spec.masterySelections[best.id] = bestEffect
			local eff = spec.tree.masteryEffects[bestEffect]
			if eff then best.sd = eff.sd best.allMasteryOptions = false spec.tree:ProcessStats(best) best.finalModList = best.modList end
			local effect = spec.tree.masteryEffects[bestEffect]
			effectText = effect and t_concat(effect.sd or {}, " / ") or tostring(bestEffect)
			t_insert(report.masteries, { node_id = best.id, mastery = best.dn, effect = bestEffect, text = effectText })
		end
		spec:AllocNode(best)
		if bestVal.enables then
			-- the node is a sustaining source for these conditions: switch them on, as the build now has the source
			for _, c in ipairs(bestVal.enables) do
				build.configTab.input[c.var] = c.value
				t_insert(report.enabled_config, { var = c.var, value = c.value, source = effectText and (best.dn .. ": " .. effectText) or best.dn })
				report.invalidated = (report.invalidated or 0) + fillInvalidateFor(scores, c.var)
			end
			build.configTab:BuildModList()
		end
		calcFunc, baseOut = calcs.getMiscCalculator(build)   -- fresh baseline for the new tree
		base = fillSummary(baseOut)
		generation = generation + 1
		report.recalcs = report.recalcs + 1
		remaining = remaining - cost
		report.points_added = report.points_added + cost
		t_insert(report.added, { name = effectText and (best.dn .. ": " .. effectText) or best.dn, type = best.type, id = best.id, cost = cost,
			via = #via > 0 and via or nil, node_ids = ids, mastery_effect = bestEffect,
			enables = bestVal.enables and (function() local l = {} for _, c in ipairs(bestVal.enables) do t_insert(l, c.var) end return l end)() or nil,
			dps_pct = bestVal.dps_pct, def_pct = bestVal.def_pct })
		::continue_loop::
	end
	measure()   -- leave PoB's output consistent with the final tree
	report.recalcs = report.recalcs + 1
	report.points_after = (spec:CountAllocNodes())
	local o = build.calcsTab.mainOutput
	for _, a in ipairs({ "Str", "Dex", "Int" }) do
		if (o["Req" .. a] or 0) > (o[a] or 0) then
			report.attribute_shortfall = (report.attribute_shortfall or "") .. ("%s %d short; "):format(a, (o["Req" .. a] or 0) - (o[a] or 0))
		end
	end
	if report.attribute_shortfall then
		report.attribute_shortfall = report.attribute_shortfall .. "fill points go to power, not attributes: see the ATTRIBUTES section"
	end
	resp.fill = report
end

-- Switch on the conditional options the build ALREADY has a sustaining source for (build mode, before the
-- fill): charge generators and buff sources in enabled gems, equipped items and allocated passives,
-- ascendancy nodes included. The audit only ever argues for turning options OFF (it reviews options that are
-- set); the fill only sees sources on the tree nodes it is about to add. Without this pass a spec with Blood
-- Rage, Power Charge On Critical, Enduring Cry or a charge-generating ascendancy node is calculated with its
-- charges off, so every "+1 to Maximum ... Charges" notable, "per charge" small and charge item measures 0 and
-- is never taken. A key the spec set itself (true or false) is left alone. Same evidence rules as the audit;
-- sources that aren't sustaining (on kill, while moving, minimum count) do not switch anything on and are
-- listed so the user can decide.
local function inferConfigFromSources()
	local input = build.configTab.input
	local lines, gems = buildSourceLines(), enabledGemNames()
	local _, varByName = Cand.conditionConfigMap()
	local set, weak = {}, {}
	local function firstSustaining(info, needLine)
		for _, e in ipairs(info.evidence or {}) do
			-- a numeric option (rage count) needs a generator line or a PoB-calculated chance; PoB's CanGain* flag
			-- alone is a gate, not a source, and would set the count to its maximum on any build that merely may gain it
			if e.sustaining and (not needLine or e.kind == "source line" or e.kind == "PoB calculation") then return e.text end
		end
	end
	local function firstAny(info)
		local e = (info.evidence or {})[1]
		return e and (e.text .. " (" .. tostring(e.trigger) .. ")") or nil
	end
	for _, var in ipairs(FILL_CONDITION_VARS) do
		if not SPEC_CONFIG[var] and not input[var]
			and not (FILL_AILMENT_ELEMENT[var] and not fillDealsElement(FILL_AILMENT_ELEMENT[var])) then
			local ok, info = pcall(conditionEvidence, var, varByName[var], lines, gems)
			if ok and info.status == "source found" then
				if info.sustaining_source then
					input[var] = true
					t_insert(set, { var = var, value = true, source = firstSustaining(info) })
				else
					t_insert(weak, { var = var, source = firstAny(info) })
				end
			end
		end
	end
	for _, nv in ipairs(FILL_NUMERIC_VARS) do
		if not SPEC_CONFIG[nv.var] and not (tonumber(input[nv.var]) and tonumber(input[nv.var]) > 0) and nv.guard() then
			local ok, info = pcall(conditionEvidence, nv.var, varByName[nv.var], lines, gems)
			local src = ok and info.status == "source found" and firstSustaining(info, true) or nil
			if src then
				input[nv.var] = nv.value()
				t_insert(set, { var = nv.var, value = input[nv.var], source = src })
			elseif ok and info.status == "source found" then
				t_insert(weak, { var = nv.var, source = firstAny(info) })
			end
		end
	end
	if #set > 0 then
		build.configTab:BuildModList()
		build.buildFlag = true
		frame()
	end
	resp.inferred_config = { set = set, not_sustaining = weak }
end

-- Attribute repair (build mode, own pathing only): the old nearest-first routes often supplied Strength /
-- Dexterity / Intelligence by accident; the tighter routes don't. When a requirement is unmet after pathing,
-- take the attribute nodes that cover it at the fewest points (a +30 node one step off the tree is the skill's
-- option 4), within the points the level allows, before the fill spends them. Reported so the model can
-- replace them with a cheaper fix (gem level, amulet base, a suffix) in the next revision.
local function repairAttributes(level)
	local spec = build.spec
	local o = build.calcsTab.mainOutput
	local report = { added = {}, points = 0 }
	local used = spec:CountAllocNodes()
	local spare = pointsAvailable(level, o.ExtraPoints or 0) - used - ((FILL and FILL.reserve) or 0)
	local delta = PATHING and PATHING.attribute_delta or {}
	-- budget: the points the search saved against nearest-first routing pay for attribute nodes first (so the
	-- build is never worse off than the old routing left it); beyond that, a shortfall is covered only up to
	-- the attributes the old route's travel supplied and this one doesn't. Anything left is the spec's own
	-- shortfall and stays an ERROR with the ordered options (the skill's attribute policy).
	local saved = PATHING and ((PATHING.nearest_points or 0) - (PATHING.total_points or 0)) or 0
	report.budget_points = saved
	local MARGIN = 10   -- the audit's target margin: leftover saved points buy it, a hard shortfall never waits for it
	for _, attr in ipairs({ "Str", "Dex", "Int" }) do
		local short = (o["Req" .. attr] or 0) - (o[attr] or 0)
		while spare > 0 and ((short > 0 and (report.points < saved or short <= (delta[attr] or 0)))
			or (short > -MARGIN and short <= 0 and report.points < saved and (o["Req" .. attr] or 0) > 0)) do
			local bestN, bestV
			for _, node in pairs(spec.nodes) do
				if not node.alloc and node.path and #node.path > 0 and #node.path <= spare and fillCandidateOK(node) then
					local gain = attrFromNode(node, attr)
					if gain > 0 then
						local v = gain / #node.path
						if not bestV or v > bestV or (v == bestV and node.id < bestN.id) then bestN, bestV = node, v end
					end
				end
			end
			if not bestN then break end
			local cost = #bestN.path
			spec:AllocNode(bestN)
			TARGET_IDS[bestN.id] = true   -- deliberate: never pruned
			spare = spare - cost
			report.points = report.points + cost
			t_insert(report.added, { attr = attr, name = bestN.dn, id = bestN.id, cost = cost, gain = attrFromNode(bestN, attr) })
			build.buildFlag = true
			frame()
			o = build.calcsTab.mainOutput
			short = (o["Req" .. attr] or 0) - (o[attr] or 0)
		end
		if short > 0 then
			if spare <= 0 then report.unmet = (report.unmet or "") .. ("%s %d short; "):format(attr, short)
			else report.spec_shortfall = (report.spec_shortfall or "") .. ("%s %d short (pathing saved %d point%s, old travel supplied %d); "):format(attr, short, saved, saved == 1 and "" or "s", math.max(0, delta[attr] or 0)) end
		end
	end
	if #report.added > 0 or report.unmet or report.spec_shortfall then resp.attribute_repair = report end
end

local function analyse(xmlText, level, expectedNodes)
	readExplicitSkillSettings(xmlText)
	stage("prepare (spec -> tree, items, XML)")
	loadBuildFromXML(xmlText, "build")
	frame()
	stage("PoB load + first calculation")
	if not build.calcsTab or not build.calcsTab.mainOutput then
		err("Path of Building failed to load the build")
		finish()
	end
	-- PoB's "Auto" character level re-derives the level from points spent at the END of a calculation
	-- (EstimatePlayerProgress), so that calculation's numbers belong to the previous level. Recalculate
	-- until the level is stable, so every reported statistic comes from one level.
	if build.characterLevelAutoMode then
		for _ = 1, 3 do
			local before = build.characterLevel
			build.configTab:BuildModList()
			build.buildFlag = true
			frame()
			if build.characterLevel == before then break end
		end
		-- PoB writes the <Build> attributes in hash order, so 'level' may come first: match it anywhere in the tag
		local buildTag = xmlText:match("<Build%s[^>]*>")
		local saved = buildTag and tonumber((" " .. buildTag):match("%slevel=\"(%d+)\""))
		if saved and saved ~= build.characterLevel then
			warn(("PoB's Auto level sets the character to level %d from the passive points spent; the build file says level %d. "
				.. "All numbers are calculated at level %d, as the PoB app shows them."):format(build.characterLevel, saved, build.characterLevel))
		end
		resp.level_info = { auto = true, saved = saved, used = build.characterLevel }
	end
	local spec = build.spec
	level = level or build.characterLevel or 90
	-- cluster jewel nodes (notables and keystones inside socketed cluster jewels) exist only now, after the
	-- items are loaded: allocate the ones the spec names (tree.cluster) through their socket
	if resp.mode == "build" and CLUSTER and #CLUSTER > 0 then
		spec:BuildAllDependsAndPaths()
		local added = {}
		for _, want in ipairs(CLUSTER) do
			local q = norm(tostring(want))
			local found
			for _, node in pairs(spec.nodes) do
				if node.dn and norm(node.dn) == q and (node.expansionJewel or (node.group and node.group.isProxy) or node.id >= 65536) and not node.alloc and node.path then
					if not found or #node.path < #found.path then found = node end
				end
			end
			if not found then
				err(("Cluster node '%s' not found in a socketed cluster jewel (is the jewel in an allocated cluster socket? exact name?)"):format(tostring(want)))
			else
				local cost = #found.path
				spec:AllocNode(found)
				t_insert(added, { name = found.dn, type = found.type, id = found.id, cost = cost })
			end
		end
		resp.plan = resp.plan or {}
		resp.plan.cluster = added
		if #added > 0 then
			build.configTab:BuildModList()
			build.buildFlag = true
			frame()
		end
	end
	if resp.mode == "build" and ATTR_REPAIR then
		local okA, aerr = pcall(repairAttributes, level)
		if not okA then warn("Attribute repair failed: " .. tostring(aerr)) end
		stage("attribute repair")
	end
	if resp.mode == "build" and INFER_CONFIG then
		local okI, ierr = pcall(inferConfigFromSources)
		if not okI then warn("Config inference from existing sources failed: " .. tostring(ierr)) end
		stage("config inference")
	end
	if FILL and resp.mode == "build" then
		local okF, ferr = pcall(fillTree, level, build.calcsTab.mainOutput.ExtraPoints or 0)
		if not okF then warn("Tree fill failed: " .. tostring(ferr)) end
		-- the fill's notables may have made some of the targets' travel redundant: prune it and spend the
		-- freed points with a second fill round (fill-chosen nodes are protected; their travel is not)
		if okF and PRUNE and resp.fill and resp.fill.added then
			local keep = {}
			for id in pairs(TARGET_IDS) do keep[id] = true end
			for _, a in ipairs(resp.fill.added) do keep[a.id] = true end
			local removed = pruneTravel(spec, protectedSet(spec, keep))
			if #removed > 0 then
				local first = resp.fill
				first.pruned_after = {}
				for _, n in ipairs(removed) do t_insert(first.pruned_after, { id = n.id, name = n.dn }) end
				build.configTab:BuildModList()
				build.buildFlag = true
				frame()
				local ok2, err2 = pcall(fillTree, level, build.calcsTab.mainOutput.ExtraPoints or 0)
				if ok2 and resp.fill ~= first then
					local second = resp.fill
					for _, a in ipairs(second.added or {}) do t_insert(first.added, a) end
					for _, m in ipairs(second.masteries or {}) do t_insert(first.masteries, m) end
					for _, c in ipairs(second.enabled_config or {}) do t_insert(first.enabled_config, c) end
					first.points_added = first.points_added + (second.points_added or 0) - #removed
					first.recalcs = first.recalcs + (second.recalcs or 0)
					first.points_after = second.points_after
					first.stopped = second.stopped
					first.refill_round = { freed = #removed, spent = second.points_added or 0 }
				elseif not ok2 then
					warn("Second tree fill after pruning failed: " .. tostring(err2))
				end
				resp.fill = first
			end
		end
		-- PoB's warnings (attribute requirements etc.) are rebuilt by a full frame, not by the fill's lighter
		-- recalculation: refresh them for the final tree, or a shortfall the fill closed is still reported
		if okF then build.buildFlag = true frame() end
		stage("tree fill")
	end
	local out = build.calcsTab.mainOutput

	-- passive tree checks
	local used, ascUsed = spec:CountAllocNodes()
	local extra = out.ExtraPoints or 0
	local avail = pointsAvailable(level, extra)
	local treeReport = {
		class = spec.curClassName, ascendancy = spec.curAscendClassName,
		bloodline = spec.curSecondaryAscendClassName ~= "None" and spec.curSecondaryAscendClassName or nil,
		tree_version = spec.treeVersion, latest_tree_version = latestTreeVersion,
		points_used = used, points_available_at_level = avail, points_max_at_100 = 99 + 23 + extra,
		ascendancy_points_used = ascUsed, ascendancy_points_available_at_level = ascPointsAvailable(level),
		keystones = {}, notables = {}, masteries = {},
	}
	for id, node in pairs(spec.allocNodes) do
		if node.type == "Keystone" then t_insert(treeReport.keystones, node.dn)
		elseif node.type == "Notable" then t_insert(treeReport.notables, node.ascendancyName and (node.dn .. " [" .. node.ascendancyName .. "]") or node.dn)
		elseif node.type == "Mastery" and spec.masterySelections[id] then
			local eff = treeObj().masteryEffects[spec.masterySelections[id]]
			t_insert(treeReport.masteries, node.dn .. ": " .. (eff and t_concat(eff.sd, " / ") or "?"))
		end
	end
	table.sort(treeReport.keystones) table.sort(treeReport.notables) table.sort(treeReport.masteries)
	treeReport.allocated_ids = {}
	for id, node in pairs(spec.allocNodes) do
		if node.type ~= "ClassStart" and node.type ~= "AscendClassStart" then t_insert(treeReport.allocated_ids, id) end
	end
	table.sort(treeReport.allocated_ids)
	if spec.treeVersion ~= latestTreeVersion then
		warn(("Tree version is %s; latest is %s. Re-check the tree after converting in PoB."):format(spec.treeVersion, latestTreeVersion))
	end
	if used > 99 + 23 + extra then
		err(("%d passive points used; the absolute maximum is %d"):format(used, 99 + 23 + extra))
	elseif used > avail then
		err(("%d passive points used but a level %d character has %d"):format(used, level, avail))
	elseif used < avail - 2 then
		if PRUNE and not FILL and resp.mode == "build" then
			warn(("%d unspent passive points at level %d: the pathing connected the targets with fewer points than nearest-first "
				.. "routing would, and this spec has no tree.fill to spend them. Set \"tree\": {\"fill\": \"balanced\"} (an unfilled tree is an unfinished build)."):format(avail - used, level))
		else
			note(("%d unspent passive points at level %d"):format(avail - used, level))
		end
	end
	if ascUsed > 8 then
		err(("%d ascendancy points used (max 8)"):format(ascUsed))
	elseif ascUsed > ascPointsAvailable(level) then
		warn(("%d ascendancy points at level %d: the matching Labyrinth is usually done later"):format(ascUsed, level))
	end
	if spec.curAscendClassId == 0 and level >= 40 then
		warn("No ascendancy selected")
	end
	if expectedNodes then
		local dropped = {}
		for id in pairs(expectedNodes) do
			local node = spec.nodes[id]
			if not spec.allocNodes[id] and node and node.type ~= "ClassStart" and node.type ~= "AscendClassStart" then
				t_insert(dropped, (node.dn or "?") .. " (" .. id .. ")")
			elseif not node then
				t_insert(dropped, "unknown node id " .. id)
			end
		end
		if #dropped > 0 then
			err("Passive nodes not connected to the class start (PoB removed them): " .. t_concat(dropped, ", "))
		end
	end
	resp.tree = treeReport

	resp.skills = reportSkills(level)
	resp.items = reportItems(level)
	-- anoints: the amulet's "Allocates X" must have allocated X (an anoint lost in item-text handling is an error)
	do
		local itemsTab = build.itemsTab
		local am = itemsTab.slots["Amulet"] and itemsTab.items[itemsTab.slots["Amulet"].selItemId]
		if am then
			for _, ml in ipairs(am.enchantModLines or {}) do
				local name = ml.line and ml.line:match("^Allocates (.+)$")
				if name then
					-- PoB grants anointed notables inside the calculation (env.allocNodes / env.grantedPassives), not on the tree
					local env = build.calcsTab.mainEnv
					local found = false
					for id, node in pairs((env and env.allocNodes) or {}) do
						if node.dn == name and ((env.grantedPassives and env.grantedPassives[id]) or not spec.allocNodes[id]) then found = true end
					end
					if not found then err(("Anoint '%s' on the amulet did not allocate that notable"):format(name)) end
					resp.anoint = name
				end
			end
		end
	end
	do
		local okA, rep = pcall(attributeReport, level, resp.items)
		if okA then resp.attributes = rep else warn("Attribute report failed: " .. tostring(rep)) end
	end
	for _, g in ipairs(resp.skills) do
		for _, p in ipairs(g.problems) do err(("Skill group %d (%s): %s"):format(g.index, g.slot or "no slot", p)) end
		-- imported builds get a warning, generated builds an error (same policy as item mods)
		for _, p in ipairs(g.trigger_problems or {}) do
			(resp.mode == "check" and warn or err)(("Skill group %d (%s): %s"):format(g.index, g.slot or "no slot", p))
		end
	end
	for _, r in ipairs(resp.items) do
		for _, p in ipairs(r.problems) do
			local msg = ("Item %s [%s]: %s"):format(r.name or "?", r.slot, p)
			-- Items in an imported build may be legitimate legacy items from older patches
			if resp.mode == "check" and not contains(p, "requires level") and not contains(p, "unknown base") then
				warn(msg .. " (legacy item, or not obtainable now)")
			else
				err(msg)
			end
		end
	end

	-- PoB's own warnings (attribute requirements, mana, reservation, etc.)
	-- Ones that make the build unusable in-game are promoted to errors.
	resp.pob_warnings = {}
	local blocking = { "do not meet the", "do not have enough", "below 1", "is negative", "too many passive points",
		"too many ascendancy points", "too many gems in your", "exceeding jewel limit", "more than one Aspect" }
	for _, w in pairs(build.controls.warnings.lines or {}) do
		w = stripColor(w)
		local isBlocking = false
		for _, b in ipairs(blocking) do if contains(w, b) then isBlocking = true break end end
		if isBlocking then
			if resp.mode == "build" and PATHING and w:find("do not meet the") then
				local attr = w:match("meet the (%a+) requirement")
				local short = { Strength = "Str", Dexterity = "Dex", Intelligence = "Int" }
				local a = attr and short[attr]
				local supplied = a and PATHING.attribute_delta and PATHING.attribute_delta[a] or 0
				local saved = (PATHING.nearest_points or 0) - (PATHING.total_points or 0)
				if (supplied > 0 or saved > 0) and not resp.pathing_attr_note then
					-- a separate note: tools match PoB's error text verbatim (the sweep's attribute-blocked retries)
					resp.pathing_attr_note = true
					note(("Attribute shortfall context: the pathing is %d point%s cheaper than nearest-first and no longer passes through travel "
						.. "worth about +%d %s. See ATTRIBUTES for the options; a named +30 node costs one of the saved points."):format(
						saved, saved == 1 and "" or "s", math.max(0, supplied), attr))
				end
			end
			err(w)
		else t_insert(resp.pob_warnings, w) end
	end
	table.sort(resp.pob_warnings)

	-- Resource sustain for the main skill used continuously (matters most for channelled skills):
	-- compare cost per second with regen + leech/on-hit and estimate time until the pool runs dry.
	resp.sustain = {}
	-- A main skill fired by an event the player doesn't control (Cast when Stunned, Cast when Damage Taken):
	-- PoB's rate is the trigger cooldown ceiling (CwS: 10 stuns a second), not play. With the spec's
	-- trigger_events_per_second the cost and the DPS are evaluated at that event rate instead.
	local costScale = 1
	do
		local ms0 = build.calcsTab.mainEnv.player.mainSkill
		local sd = ms0 and ms0.skillData or {}
		local kind = sd.chanceToTriggerOnStun and "stun" or (sd.triggeredByDamageTaken and "damage taken threshold") or nil
		local pobRate = tonumber(out.Speed) or 0
		if kind and pobRate > 0 then
			local chance = (sd.chanceToTriggerOnStun or 100) / 100
			local ev = { kind = kind, chance = round(chance * 100, 1), pob_rate = round(pobRate, 2) }
			if TRIGGER_EVENTS then
				local rate = math.min(pobRate, TRIGGER_EVENTS * chance)
				costScale = rate / pobRate
				ev.events_per_second, ev.rate = TRIGGER_EVENTS, round(rate, 2)
				ev.hit_dps = round((tonumber(out.TotalDPS) or 0) * costScale, 1)
				ev.combined_dps = round((tonumber(out.CombinedDPS) or 0) * costScale, 1)
				ev.full_dps = out.FullDPS and round((tonumber(out.FullDPS) or 0) * costScale, 1) or nil
			else
				warn(("The main skill is triggered when you %s: PoB assumes %.2f triggers/s (the trigger cooldown ceiling), not a rate you control. "
					.. "Set \"trigger_events_per_second\" in the spec to the %s rate you expect; DPS and cost are then evaluated at it.")
					:format(kind == "stun" and "are stunned" or "take damage", pobRate, kind))
			end
			resp.trigger_events = ev
		end
	end
	for _, pool in ipairs({
		{ name = "Mana", cost = "ManaPerSecondCost", has = "ManaPerSecondHasCost", regen = { "ManaRegenRecovery", "ManaLeechGainRate" }, size = "ManaUnreserved" },
		{ name = "Life", cost = "LifePerSecondCost", has = "LifePerSecondHasCost", regen = { "LifeRegenRecovery", "LifeLeechGainRate" }, size = "LifeUnreserved" },
		{ name = "Energy Shield", cost = "ESPerSecondCost", has = "ESPerSecondHasCost", regen = { "EnergyShieldRegenRecovery", "EnergyShieldLeechGainRate" }, size = "EnergyShield" },
	}) do
		local cost = (out[pool.has] and tonumber(out[pool.cost]) or 0) * costScale
		if cost > 0 then
			local rec = 0
			for _, k in ipairs(pool.regen) do rec = rec + (tonumber(out[k]) or 0) end
			local size = tonumber(out[pool.size]) or 0
			local entry = { pool = pool.name, cost_per_second = round(cost, 1), recovery_per_second = round(rec, 1) }
			if cost > rec then
				entry.seconds_until_empty = round(size / (cost - rec), 1)
				local msg = ("Main skill costs %.0f %s/s but you recover %.0f/s: %s runs dry after ~%.0fs of continuous use")
					:format(cost, pool.name, rec, pool.name, entry.seconds_until_empty)
				if entry.seconds_until_empty < 8 then err(msg .. " (unsustainable)") else warn(msg) end
			end
			t_insert(resp.sustain, entry)
		end
	end

	local mainSkill = build.calcsTab.mainEnv.player.mainSkill
	resp.main_skill = mainSkill and mainSkill.activeEffect and mainSkill.activeEffect.grantedEffect and mainSkill.activeEffect.grantedEffect.name or nil
	do
		local gd = mainSkill and mainSkill.activeEffect and mainSkill.activeEffect.srcInstance and mainSkill.activeEffect.srcInstance.gemData
		if gd then resp.main_skill_gem = { name = gd.name, variant_id = gd.variantId, game_id = gd.gameId } end
	end
	resp.main_skill_config = mainSkillConfig(mainSkill)
	local msc = resp.main_skill_config
	-- generated builds must be right (error); imported builds are reported (warning), like item mods
	local problem = resp.mode == "check" and warn or err
	if msc then
		local want = explicitStages[msc.name]
		if want then
			if not msc.stages then
				problem(("Main skill %s: a stage count is set but PoB calculates no stages for it in this build"):format(msc.name))
			elseif want > msc.stages.max then
				problem(("Main skill %s: %d stages requested but this build's maximum is %d (PoB would silently use %d)"):format(
					msc.name, want, msc.stages.max, msc.stages.max))
			elseif want < msc.stages.min then
				problem(("Main skill %s: %d stages requested but the minimum is %d"):format(msc.name, want, msc.stages.min))
			end
		end
	end
	if mainSkill and mainSkill.skillFlags and mainSkill.skillFlags.disable then
		err("Main skill is disabled: " .. tostring(mainSkill.disableReason))
	end
	resp.level = level
	resp.stats = collectStats(out)
	-- the sidebar list is only rebuilt by PoB's full frame; MAIN-only recalculations (tree fill) leave it stale
	pcall(function() build:RefreshStatList() end)
	resp.sidebar = sidebarLines()
	resp.xml = build:SaveDB("final")
	resp.summary = measure()
	resp.summary.points_used = used
	if resp.mode == "check" then
		local ok, res = pcall(compareSavedStats, xmlText)
		if ok then resp.saved_vs_recalculated = res else warn("Saved-stat comparison failed: " .. tostring(res)) end
		if res and ok and #res.differing > 0 then
			warn(("Saved export stats differ from PoB's recalculation by more than %d%%: %s. %s"):format(SAVED_STAT_TOLERANCE_PCT,
				t_concat(res.differing, ", "), res.explained_by and ("Reproduced by " .. res.explained_by .. ".")
				or ((res.partly_explained_by and ("Partly reproduced by " .. res.partly_explained_by.change .. " (matches "
					.. t_concat(res.partly_explained_by.resolves, ", ") .. "). ") or "") .. res.unexplained)))
		end
	end
	stage("validation reports + export")
	if AUDIT then
		if #resp.errors > 0 and not REFERENCE then
			-- an invalid build's contributions aren't meaningful: save the ~20 recalculations
			resp.audit_skipped = "build is invalid; fix the errors first"
		else
			-- reference analysis measures an invalid build too, and says so
			if #resp.errors > 0 then resp.audit_on_invalid_build = true end
			local ok, res = pcall(runAudits)
			if ok then resp.audit = res else warn("Audit failed: " .. tostring(res)) end
			stage("quality audit")
		end
	end
	local okP, prof = pcall(Cand.profile, false)
	if okP then resp.build_profile = prof else warn("Build profile failed: " .. tostring(prof)) end
	if Cand.request then
		if #resp.errors > 0 then
			resp.candidates_skipped = "build is invalid; fix the errors first"
		else
			local okC, res = pcall(Cand.run, Cand.request)
			if okC then resp.candidates = res else warn("Candidate search failed: " .. tostring(res)) end
			stage("candidate search")
		end
	end
end

---------------------------------------------------------------------------
-- Lookup modes
---------------------------------------------------------------------------
local function modeNodes(req)
	local q = req.query or ""
	local typeFilter = req.type and lc(req.type)
	local spec = build.spec
	if req.class then
		local cid = findClass(req.class)
		if cid then
			spec:SelectClass(cid)
			if req.ascendancy then
				local aid = findAscendancy(spec.curClass, req.ascendancy)
				if aid then spec:SelectAscendClass(aid) end
			end
		else
			err("Unknown class '" .. req.class .. "'")
		end
	end
	local results = {}
	for id, node in pairs(spec.nodes) do
		if isRealNode(node) then
			local hay = (node.dn or "") .. " " .. t_concat(nodeStats(node), " ")
			local typeOk = not typeFilter or lc(node.type) == typeFilter
			local ascOk = (node.ascendancyName == nil) or (req.ascendancy and norm(node.ascendancyName) == norm(req.ascendancy)) or req.include_ascendancies
			if typeOk and ascOk and (q == "" or contains(hay, q)) and not (node.type == "Normal" and q == "" ) then
				t_insert(results, {
					id = id, name = node.dn, type = node.type, ascendancy = node.ascendancyName,
					stats = nodeStats(node), distance = node.path and #node.path or nil,
				})
			end
		end
	end
	table.sort(results, function(a, b)
		local da, db = a.distance or 999, b.distance or 999
		if da ~= db then return da < db end
		return tostring(a.name) < tostring(b.name)
	end)
	local limit = tonumber(req.limit) or 40
	local out = {}
	for i = 1, math.min(limit, #results) do out[i] = results[i] end
	resp.results = out
	resp.total = #results
end

local function modeMasteries(req)
	local q = req.query or ""
	local spec = build.spec
	if req.class then
		local cid = findClass(req.class)
		if cid then spec:SelectClass(cid) end
	end
	local results, byName = {}, {}
	for id, node in pairs(spec.nodes) do
		if node.type == "Mastery" and node.masteryEffects and isRealNode(node) then
			local entry = byName[node.dn]
			if not entry then
				local effects = {}
				for _, eff in ipairs(node.masteryEffects) do
					local e = treeObj().masteryEffects[eff.effect]
					local text = e and t_concat(e.sd, " / ") or ""
					if q == "" or contains(text, q) or contains(node.dn, q) then t_insert(effects, stripColor(text)) end
				end
				if #effects > 0 then
					entry = { mastery = node.dn, effects = effects, clusters = {} }
					byName[node.dn] = entry
					t_insert(results, entry)
				end
			end
			if entry then
				-- the notables that unlock this mastery node (its linked notables), with distance from the class start
				local names, dist = {}, nil
				for _, other in ipairs(node.linked or {}) do
					if other.type == "Notable" and other.dn then
						t_insert(names, other.dn)
						if other.path then dist = math.min(dist or 999, #other.path) end
					end
				end
				table.sort(names)
				if #names > 0 then t_insert(entry.clusters, { id = id, notables = names, distance = dist }) end
			end
		end
	end
	for _, e in ipairs(results) do
		table.sort(e.clusters, function(a, b) return (a.distance or 999) < (b.distance or 999) end)
	end
	table.sort(results, function(a, b) return a.mastery < b.mastery end)
	resp.results = results
end

local function modeGems(req)
	local q = req.query or ""
	local results = {}
	for id, g in pairs(build.data.gems) do
		local ge = g.grantedEffect
		local tags = {}
		for k, v in pairs(g.tags or {}) do if v == true then t_insert(tags, k) end end
		table.sort(tags)
		local tagStr = t_concat(tags, ",")
		if (q == "" or contains(g.name, q) or (req.search_tags and contains(tagStr, q)))
			and (req.support == nil or (ge.support and true or false) == req.support) then
			t_insert(results, {
				name = g.name, support = ge.support or false, color = gemColorLetter(g), tags = tags,
				stages = (stageInfo(ge)) or nil, parts = (#partNames(ge) > 1) and partNames(ge) or nil,
				max_level = g.naturalMaxLevel, req_str = g.reqStr, req_dex = g.reqDex, req_int = g.reqInt,
				description = ge.description and stripColor(ge.description) or nil,
				transfigured = g.variantId and g.variantId:find("Alt") and true or nil,
			})
		end
	end
	table.sort(results, function(a, b) return a.name < b.name end)
	local limit = tonumber(req.limit) or 40
	local out = {}
	for i = 1, math.min(limit, #results) do out[i] = results[i] end
	resp.results, resp.total = out, #results
end

local function modeUniques(req)
	local q = req.query or ""
	local results = {}
	for title, item in pairs(uniquesByTitle()) do
		local hay = item.title .. " " .. (item.baseName or "") .. " " .. (item.type or "")
		if req.full then
			local lines = {}
			for _, ml in ipairs(item.explicitModLines or {}) do t_insert(lines, ml.line) end
			hay = hay .. " " .. t_concat(lines, " ")
		end
		if q == "" or contains(hay, q) then
			local entry = { name = item.title, base = item.baseName, type = item.type, req_level = item.requirements and item.requirements.level }
			if req.full or (req.exact and norm(item.title) == norm(q)) then
				entry.text = item:BuildRaw()
				entry.variants = item.variantList
			end
			t_insert(results, entry)
		end
	end
	table.sort(results, function(a, b) return a.name < b.name end)
	local limit = tonumber(req.limit) or 30
	local out = {}
	for i = 1, math.min(limit, #results) do out[i] = results[i] end
	resp.results, resp.total = out, #results
end

local function modeBases(req)
	local q = req.query or ""
	local results = {}
	for name, base in pairs(build.data.itemBases) do
		if (q == "" or contains(name, q) or contains(base.type or "", q) or contains(base.subType or "", q)) and not base.hidden then
			t_insert(results, {
				name = name, type = base.type, subType = base.subType, socket_limit = base.socketLimit,
				req = base.req, implicit = base.implicit, armour = base.armour, weapon = base.weapon and {
					PhysicalMin = base.weapon.PhysicalMin, PhysicalMax = base.weapon.PhysicalMax, AttackRateBase = base.weapon.AttackRateBase, CritChanceBase = base.weapon.CritChanceBase,
				} or nil,
			})
		end
	end
	table.sort(results, function(a, b) return (a.req and a.req.level or 0) > (b.req and b.req.level or 0) end)
	local limit = tonumber(req.limit) or 30
	local out = {}
	for i = 1, math.min(limit, #results) do out[i] = results[i] end
	resp.results, resp.total = out, #results
end

local function modeMods(req)
	local baseName = req.base
	local raw = "Rarity: RARE\nProbe\n" .. tostring(baseName) .. "\nItem Level: " .. tostring(req.ilvl or 86)
	for _, inf in ipairs(req.influences or {}) do raw = raw .. "\n" .. inf .. " Item" end
	local item = new("Item", raw)
	if not item.base then
		err("Unknown base '" .. tostring(baseName) .. "'")
		return
	end
	local ilvl = tonumber(req.ilvl) or 86
	local q = req.query or ""
	local groups = {}
	local pool = item.affixes or build.data.itemMods.Item
	local isAffix = function(mod) return mod.type == "Prefix" or mod.type == "Suffix" end
	if req.eldritch then
		-- Searing Exarch / Eater of Worlds implicits; "best tier" = highest values
		pool = build.data.itemMods.Eldritch or {}
		isAffix = function(mod) return mod.type == "Exarch" or mod.type == "Eater" end
		ilvl = 100
	elseif req.corrupted then
		-- Vaal orb implicits for this item class (also valid for corrupted uniques of the class)
		pool = {}
		for _, src in ipairs(corruptedPools()) do
			for id, mod in pairs(src) do if mod.type == "Corrupted" then pool[id] = mod end end
		end
		isAffix = function(mod) return mod.type == "Corrupted" end
		ilvl = 100
	end
	for id, mod in pairs(pool) do
		if isAffix(mod) and item:GetModSpawnWeight(mod) > 0 then
			local text = t_concat(modLines(mod), " / ")
			if q == "" or contains(text, q) then
				local key = mod.type .. "|" .. (mod.group or text)
				local g = groups[key]
				if not g then
					g = { type = mod.type, group = mod.group, tiers = {} }
					groups[key] = g
				end
				local _, ranges = templateToPattern(modLines(mod)[1] or "")
				local top = ranges[1] and math.max(math.abs(ranges[1][1]), math.abs(ranges[1][2])) or tonumber(text:match("%d+%.?%d*")) or 0
				t_insert(g.tiers, { level = mod.level or 1, top = top, text = text, name = mod.affix, available = (mod.level or 1) <= ilvl })
			end
		end
	end
	local results = {}
	for _, g in pairs(groups) do
		table.sort(g.tiers, function(a, b)
			if a.level ~= b.level then return a.level > b.level end
			return a.top > b.top
		end)
		local best
		for _, t in ipairs(g.tiers) do if t.available then best = t break end end
		if best then
			t_insert(results, { type = g.type, group = g.group, best_tier = best.text, best_tier_level = best.level, tier_count = #g.tiers })
		end
	end
	table.sort(results, function(a, b)
		if a.type ~= b.type then return a.type < b.type end
		return tostring(a.group) < tostring(b.group)
	end)
	local crafts = {}
	if req.crafted ~= false then
		for _, mod in ipairs(build.data.masterMods) do
			if mod.types and mod.types[item.base.type] and (mod.type == "Prefix" or mod.type == "Suffix") then
				local text = t_concat(modLines(mod), " / ")
				if q == "" or contains(text, q) then
					t_insert(crafts, { type = mod.type, text = text })
				end
			end
		end
	end
	resp.base = { name = item.baseName, type = item.base.type, subType = item.base.subType, item_level = ilvl, implicit = item.base.implicit }
	resp.results = results
	resp.crafted = crafts
end

local function modeClasses()
	local out = {}
	for id, class in pairs(treeObj().classes) do
		local ascs = {}
		for aid, asc in pairs(class.classes) do
			if aid ~= 0 then t_insert(ascs, asc.name) end
		end
		table.sort(ascs)
		t_insert(out, { class = class.name, ascendancies = ascs })
	end
	table.sort(out, function(a, b) return a.class < b.class end)
	local bloodlines = {}
	for _, asc in pairs(treeObj().alternate_ascendancies or {}) do t_insert(bloodlines, asc.name) end
	table.sort(bloodlines)
	resp.results = out
	resp.bloodlines = bloodlines
	resp.tree_version = build.spec.treeVersion
end

local function modeConfig(req)
	local q = req.query or ""
	local out = {}
	local varList = LoadModule("Modules/ConfigOptions")
	for _, opt in ipairs(varList or {}) do
		if opt.var and (q == "" or contains(opt.var, q) or contains(opt.label or "", q)) then
			local entry = { var = opt.var, type = opt.type, label = opt.label and stripColor(opt.label) or nil }
			if opt.list then
				entry.values = {}
				for _, v in ipairs(opt.list) do t_insert(entry.values, tostring(v.val)) end
			end
			t_insert(out, entry)
		end
	end
	resp.results = out
end

---------------------------------------------------------------------------
-- Static name index for the Python preflight (mode "names")
---------------------------------------------------------------------------
local function modeNames(req)
	local out = { classes = {}, gems = {}, uniques = {}, bases = {}, nodes = {}, ascendancy_nodes = {}, ascendancy_node_ids = {},
		config = {}, bloodlines = {}, tree_version = build.spec.treeVersion }
	for _, class in pairs(treeObj().classes) do
		local ascs = {}
		for id, asc in pairs(class.classes) do if id ~= 0 then t_insert(ascs, asc.name) end end
		table.sort(ascs)
		out.classes[class.name] = ascs
	end
	for _, asc in pairs(treeObj().alternate_ascendancies or {}) do t_insert(out.bloodlines, asc.name) end
	for _, gd in pairs(build.data.gems) do
		local levels = {}
		local ge = gd.grantedEffect
		for l = 1, (gd.naturalMaxLevel or 20) + 1 do
			local lv = ge and ge.levels and ge.levels[l]
			levels[l] = lv and lv.levelRequirement or json.null
		end
		local staged, baseMax = stageInfo(ge)
		local tags = {}
		for k, v in pairs(gd.tags or {}) do if v == true then t_insert(tags, k) end end
		table.sort(tags)
		t_insert(out.gems, { name = gd.name, max = gd.naturalMaxLevel or 20, req = levels,
			support = ge and ge.support or false, stages = staged, stages_base_max = baseMax, parts = partNames(ge),
			variant_id = gd.variantId, game_id = gd.gameId, tags = tags })
	end
	for _, it in pairs(uniquesByTitle()) do t_insert(out.uniques, it.title) end
	for name in pairs(build.data.itemBases) do t_insert(out.bases, name) end
	for id, node in pairs(build.spec.nodes) do
		if isRealNode(node) and node.dn then
			if node.ascendancyName then
				out.ascendancy_nodes[node.ascendancyName] = out.ascendancy_nodes[node.ascendancyName] or {}
				t_insert(out.ascendancy_nodes[node.ascendancyName], node.dn)
				out.ascendancy_node_ids[tostring(id)] = node.dn
			else
				out.nodes[tostring(id)] = node.dn
			end
		end
	end
	for _, v in ipairs(LoadModule("Modules/ConfigOptions") or {}) do
		if v.var then
			local vals
			if v.list then vals = {} for _, l in ipairs(v.list) do t_insert(vals, tostring(l.val)) end end
			out.config[v.var] = { type = v.type, values = vals }
		end
	end
	-- test hook: resolve names with PoB's own matcher (used to check the Python port)
	if req.resolve_gems then
		out.resolved = {}
		for _, name in ipairs(req.resolve_gems) do
			local gd = findGem(name)
			t_insert(out.resolved, gd and gd.name or json.null)
		end
	end
	resp.results = out
end

---------------------------------------------------------------------------
-- Main
---------------------------------------------------------------------------
local function resetResp()
	for k in pairs(resp) do resp[k] = nil end
	resp.errors, resp.warnings, resp.notes = {}, {}, {}
end

local function runRequest(req)
	if type(req) ~= "table" then error("request is not a JSON object") end
	local mode = req.mode
	resp.mode = mode
	resp.pob_version = launch.versionNumber
	AUDIT = req.audit and true or false
	REFERENCE = req.reference and true or false
	TIMING = req.timing and true or false
	Cand.request = type(req.candidates) == "table" and req.candidates or nil
	stageStart = os.clock()
	if mode ~= "build" then newBuild() end   -- buildFromSpec makes its own fresh build
	resp.latest_tree_version = latestTreeVersion
	if mode == "build" then
		local xmlText, level = buildFromSpec(req.spec or {})
		local expected = parseSpecNodes(xmlText)
		analyse(xmlText, level, expected)
	elseif mode == "check" then
		local xmlText = req.xml
		local expected = parseSpecNodes(xmlText)
		if not expected then err("Could not read a passive tree from this build") end
		analyse(xmlText, nil, expected)
	elseif mode == "nodes" then modeNodes(req)
	elseif mode == "masteries" then modeMasteries(req)
	elseif mode == "gems" then modeGems(req)
	elseif mode == "uniques" then modeUniques(req)
	elseif mode == "bases" then modeBases(req)
	elseif mode == "mods" then modeMods(req)
	elseif mode == "classes" then modeClasses()
	elseif mode == "config" then modeConfig(req)
	elseif mode == "names" then modeNames(req)
	else
		err("Unknown mode '" .. tostring(mode) .. "'")
	end
	stage("lookup / finish")
end

local function runGuarded(req)
	local ok, runErr = pcall(runRequest, req)
	if not ok and runErr ~= FINISH then
		err("Driver error: " .. tostring(runErr))
	end
	resp.valid = #resp.errors == 0
end

local startupSeconds = os.clock()
local req = json.decode(readFile(REQ_PATH) or "")
if type(req) == "table" and req.mode == "batch" then
	-- several requests, one PoB start-up; each gets its own fresh response and build
	BATCH = true
	local results = {}
	for i, sub in ipairs(req.requests or {}) do
		resetResp()
		runGuarded(sub)
		local r = {}
		for k, v in pairs(resp) do r[k] = v end
		results[i] = r
	end
	BATCH = false
	writeFile(RESP_PATH, json.encode({ mode = "batch", results = results, startup_seconds = startupSeconds }, { indent = true }))
	os.exit(0)
end
runGuarded(req)
if req and req.timing then resp.startup_seconds = startupSeconds end
finish()