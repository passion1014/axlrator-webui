<script lang="ts">
	import CodeAssistChat from '$lib/components/chat/CodeAssistChat.svelte';
	//AXL:김정민 일단 없어서 주석처리
	// import Help from '$lib/components/layout/Help.svelte';

	import { onMount } from 'svelte';
	import { temporaryChatEnabled } from '$lib/stores';

	onMount(() => {
		//AXL:김정민: /codeassist/ 페이지가 로드될 때 임시 채팅 기능을 활성화합니다.
		temporaryChatEnabled.set(true);
	});
</script>

<!-- <Help /> -->
<CodeAssistChat />
